import struct
import unittest

from replay_orders import ACTION_NAMES, ReplayOrderError, decode_cancel_order, decode_item_finished_order, decode_change_production_order, decode_smoke_cancel_order, decode_synchro_service_order, decode_production_order, production_order_size, walk_player_orders


GUID = bytes.fromhex("00112233445566778899aabbccddeeff")
TRANSPORT = bytes.fromhex("fedcba98765432100123456789abcdef")


def u32(value):
    return struct.pack("<I", value)


def blob(payload):
    return u32(11) + u32(len(payload)) + payload


def production(subtype=1, quantity=1, order=b"None"):
    pack = u32(1) + GUID + TRANSPORT + bytes(16) + struct.pack("<4I", 1, 3, 0xFFFFFFFF, 4)
    base = blob(u32(subtype)) + struct.pack("<II", 0xC0000020, 0) + blob(b"") + blob(pack)
    if subtype == 2:
        tail = struct.pack("<II", quantity, 1)
    else:
        tail = u32(quantity) + b"".join(struct.pack("<fffI", 1.0, 2.0, 3.0, 0) for _ in range(quantity))
        tail += struct.pack("<II", quantity, 1) + b"\x00\x01" + u32(quantity)
        tail += b"".join(u32(1) + u32(100 + index) for index in range(quantity))
        tail += b"\x00" + u32(17) + u32(len(order)) + order
    return b"\x0f" + u32(subtype) + blob(base + tail)


def cancel():
    return b"\x0d" + u32(0xC0000020) + struct.pack("<4I", 0, 0, 0, 50) + u32(4) + blob(u32(100)) + blob(u32(0))


def standard_order(selection=1, extra_selection=0, tail=b""):
    raw = b"\x05" + u32(selection) + b"".join(u32(0xC0000040 + index) for index in range(selection))
    raw += u32(extra_selection) + bytes(extra_selection * 4) + bytes(56) + u32(0)
    raw += blob(u32(0)) + struct.pack("<H", len(tail)) + tail
    return raw


class OrderTests(unittest.TestCase):
    def test_ground_production_reads_asset_guids_and_requested_groups(self):
        raw = production(quantity=10)
        self.assertEqual(len(raw), 388)
        result = decode_production_order(raw)
        self.assertEqual(result["unit_guids"], [GUID.hex()])
        self.assertEqual(result["transport_guid"], TRANSPORT.hex())
        self.assertIsNone(result["super_transport_guid"])
        self.assertEqual(result["requested_count"], 10)
        self.assertEqual(result["placement_count"], 10)
        self.assertEqual(result["production_groups"], [[100 + index] for index in range(10)])
        self.assertEqual(result["evidence"], "production_request")
        self.assertFalse(result["deployment_verified"])
        self.assertNotIn("positions", result)

    def test_air_and_alternate_ground_subtypes(self):
        air = production(subtype=2)
        self.assertEqual(len(air), 125)
        self.assertEqual(decode_production_order(air)["requested_count"], 1)
        ground = production(subtype=3, order=b"MoveFast")
        self.assertEqual(decode_production_order(ground)["subtype"], 3)

    def test_air_world_state_two_is_one_requested_pack(self):
        result = decode_production_order(production(subtype=2, quantity=2))
        self.assertEqual(result["production_values"][0], 2)
        self.assertEqual(result["air_production_state"], 2)
        self.assertEqual(result["requested_count"], 1)
        with self.assertRaises(ReplayOrderError) as captured:
            decode_production_order(production(subtype=2, quantity=3))
        self.assertEqual(captured.exception.code, "unsupported_air_production_state")

    def test_item_finished_is_typed_without_claiming_spawn(self):
        raw = b"\x11" + struct.pack("<6I", 0, 0xC0000020, 0, 0, 0, 50)
        result = decode_item_finished_order(raw)
        self.assertEqual(result["item_value"], 0)
        self.assertEqual(result["receiver_object_id"], 0xC0000020)
        self.assertEqual(result["receiver_context"], [0, 0, 0, 50])
        self.assertFalse(result["deployment_verified"])

    def test_smoke_cancel_and_sync_do_not_become_unit_events(self):
        smoke = decode_smoke_cancel_order(b"\x0c" + struct.pack("<6I", 0xC0000020, 0, 0, 0, 50, 4))
        service = decode_synchro_service_order(b"\x16" + struct.pack("<3I", 0, 1, 16))
        self.assertEqual(smoke["reference_value"], 4)
        self.assertEqual(service["service_values"], [0, 1, 16])
        self.assertFalse(smoke["deployment_verified"])
        self.assertFalse(service["deployment_verified"])

    def test_change_production_reads_reference_vectors_but_discards_locations(self):
        raw = b"\x0e" + u32(0xC0000020) + struct.pack("<4I", 0, 0, 0, 50)
        raw += u32(2) + blob(u32(7)) + blob(u32(9)) + u32(2) + struct.pack("<2I", 3, 4)
        raw += u32(2) + bytes(32) + u32(1) + bytes(4) + bytes(20)
        result = decode_change_production_order(raw)
        self.assertEqual(result["reference_values"], [7, 9])
        self.assertEqual(result["secondary_values"], [3, 4])
        self.assertEqual(result["placement_count"], 2)
        self.assertNotIn("positions", result)
        self.assertFalse(result["deployment_verified"])

    def test_production_boundary_handles_following_command(self):
        raw = production() + standard_order()
        self.assertEqual(production_order_size(raw), 172)
        with self.assertRaises(ReplayOrderError):
            decode_production_order(raw)

    def test_multi_command_walker_does_not_scan_embedded_opcode_bytes(self):
        nested = b"\x0f" + bytes(90)
        raw = b"\x03" + bytes(36) + production() + standard_order(2, 1, nested) + cancel()
        result = walk_player_orders(raw, 4)
        self.assertTrue(result["complete"])
        self.assertEqual(result["action_counts"], {3: 1, 15: 1, 5: 1, 13: 1})
        self.assertEqual(result["parsed_bytes"], len(raw))
        self.assertEqual(len(result["production_orders"]), 1)
        self.assertEqual(len(result["cancellation_orders"]), 1)

    def test_fixed_and_variable_verified_shapes(self):
        packets = [
            b"\x02" + bytes(21), b"\x03" + bytes(36),
            standard_order(), standard_order(2, 1, bytes(91)),
            b"\x06" + bytes(16) + u32(2) + bytes(8),
            b"\x08" + u32(2) + bytes(8) + u32(5),
            b"\x0b" + bytes(36) + struct.pack("<H", 4) + bytes(4),
            b"\x0c" + bytes(24), cancel(),
            b"\x0e" + bytes(20) + u32(1) + blob(u32(100)) + u32(1) + bytes(4)
            + u32(1) + bytes(16) + u32(1) + bytes(4) + bytes(20),
            production(), b"\x11" + bytes(24), b"\x16" + bytes(12),
            b"\x09" + bytes(8),
            b"\x27" + bytes(4) + u32(2) + bytes(16) + bytes(1),
            b"\x28" + bytes(9), b"\x29" + bytes(8), b"\x2a" + bytes(9),
        ]
        result = walk_player_orders(b"".join(packets), len(packets))
        self.assertTrue(result["complete"], result["errors"])
        self.assertEqual(result["parsed_command_count"], len(packets))

    def test_deactivate_supply_is_a_fixed_ten_byte_action(self):
        # Action 40 is "TNetworkActionDeactivateSupply": u8 opcode, u32, u32,
        # u8 - proven by its official serializer. Replays that report supply
        # deactivation must not stop the walk (that would void identity checks).
        packet = b"\x28" + u32(1) + u32(2) + b"\x00"
        result = walk_player_orders(packet + standard_order(), 2)
        self.assertTrue(result["complete"], result["errors"])
        self.assertEqual(result["action_counts"], {40: 1, 5: 1})
        self.assertEqual(ACTION_NAMES[40], "DeactivateSupply")
        truncated = walk_player_orders(packet[:-1], 1)
        self.assertFalse(truncated["complete"])

    def test_unnamed_nine_byte_action_keeps_the_walk_aligned(self):
        # Real packet captured from a 10v10 replay that previously decoded as
        # "partial_production_request_stream": one CommitOptionPlayer followed
        # by five 9-byte records of the unnamed action 9. Stopping at the first
        # of them voided every identity binding, so the whole match was dropped.
        packet = (
            b"\x03" + bytes(36)
            + b"\x09" + struct.pack("<II", 0xC000002A, 0)
            + b"\x09" + struct.pack("<II", 0xC000002A, 3)
            + b"\x09" + struct.pack("<II", 0xC000002A, 4)
            + b"\x09" + struct.pack("<II", 0xC000002A, 7)
            + b"\x09" + struct.pack("<II", 0xC000002A, 11)
        )
        self.assertEqual(len(packet), 82)
        result = walk_player_orders(packet, 6)
        self.assertTrue(result["complete"], result["errors"])
        self.assertEqual(result["action_counts"], {3: 1, 9: 5})
        self.assertEqual(result["parsed_bytes"], len(packet))
        # A 9-byte record is a boundary only: it must never decode as an order.
        self.assertEqual(result["production_orders"], [])
        self.assertEqual(result["cancellation_orders"], [])
        truncated = walk_player_orders(packet[:-1], 6)
        self.assertFalse(truncated["complete"])

    def test_cancel_request_does_not_assert_refund(self):
        result = decode_cancel_order(cancel())
        self.assertEqual(result["request_values"], [4, 100, 0])
        self.assertFalse(result["refund_verified"])

    def test_unknown_opcode_stops_without_guessing_later_buy_bytes(self):
        raw = b"\xff" + production()
        result = walk_player_orders(raw, 2)
        self.assertFalse(result["complete"])
        self.assertEqual(result["parsed_command_count"], 0)
        self.assertEqual(result["production_orders"], [])
        self.assertEqual(result["errors"], {"unsupported_player_action": 1})

    def test_all_byte_truncations_fail_instead_of_yielding_partial_production(self):
        raw = production(quantity=2)
        for cutoff in range(len(raw)):
            with self.subTest(cutoff=cutoff):
                with self.assertRaises(ReplayOrderError):
                    decode_production_order(raw[:cutoff])

    def test_blob_lengths_and_array_counts_are_bounded(self):
        raw = bytearray(production())
        struct.pack_into("<I", raw, 9, 0xFFFFFFFF)
        with self.assertRaises(ReplayOrderError):
            decode_production_order(raw)
        result = walk_player_orders(b"\x05" + u32(0xFFFFFFFF), 1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["errors"], {"oversized_order_vector": 1})

    def test_count_and_trailing_bytes_must_agree(self):
        self.assertFalse(walk_player_orders(production(), 2)["complete"])
        result = walk_player_orders(production() + b"x", 1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["errors"], {"unexpected_packet_trailing_bytes": 1})

    def test_pack_and_outer_subtypes_must_agree(self):
        raw = bytearray(production())
        struct.pack_into("<I", raw, 21, 2)
        with self.assertRaises(ReplayOrderError) as captured:
            decode_production_order(raw)
        self.assertEqual(captured.exception.code, "production_subtype_mismatch")

    def test_invalid_utf8_order_name_is_not_published(self):
        with self.assertRaises(ReplayOrderError):
            decode_production_order(production(order=b"\xff"))


if __name__ == "__main__":
    unittest.main()
