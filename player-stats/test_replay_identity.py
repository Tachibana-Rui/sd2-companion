import unittest

from replay_identity import allowed_deck_guids, bind_players


G1, G2, TRANSPORT, ACE = "1" * 32, "2" * 32, "3" * 32, "4" * 32


def stream(sources=range(2), orders=()):
    return {
        "framing_status": "validated",
        "production_decoding_status": "complete_production_request_stream",
        "incomplete_order_packet_count": 0,
        "order_decode_errors": {},
        "parsed_player_command_count": 2,
        "raw_command_count": 2,
        "player_slots": {str(slot): {} for slot in sources},
        "production_orders": list(orders),
        "cancellation_orders": [],
    }


def production(source, guid, *, owner=None, transport=None):
    return {"action_type": 15, "source_slot": source, "unit_guids": [guid],
            "transport_guid": transport, "super_transport_guid": None,
            "owner_object_id": [owner if owner is not None else 0xC000001E + source, 2]}


class IdentityTests(unittest.TestCase):
    def test_dense_header_uses_rank_and_ignores_json_insertion_order(self):
        players = [(1, {"PlayerAlliance": "1"}), (0, {"PlayerAlliance": "0"})]
        result = bind_players(players, stream())
        self.assertTrue(result["validated"])
        self.assertEqual(result["bindings"], {0: 0, 1: 1})

    def test_sparse_public_header_sorts_alliance_then_numeric_key(self):
        players = [(313, {"PlayerAlliance": "1"}), (394, {"PlayerAlliance": "1"}),
                   (392, {"PlayerAlliance": "0"}), (339, {"PlayerAlliance": "0"})]
        result = bind_players(players, stream(range(4)))
        self.assertEqual(result["bindings"], {0: 339, 1: 392, 2: 313, 3: 394})

    def test_explicit_player_slot_and_observer_precede_alliance(self):
        players = [(4, {"PlayerAlliance": 0, "PlayerSlot": 1, "PlayerIsObserver": 0}),
                   (2, {"PlayerAlliance": 1, "PlayerSlot": 0, "PlayerIsObserver": 1}),
                   (3, {"PlayerAlliance": 1, "PlayerSlot": 0, "PlayerIsObserver": 0})]
        self.assertEqual(bind_players(players, stream(range(3)))["bindings"], {0: 3, 1: 2, 2: 4})

    def test_same_deck_does_not_make_official_rank_ambiguous(self):
        players = [(10, {"PlayerAlliance": 0}), (20, {"PlayerAlliance": 0})]
        data = stream(orders=[production(0, G1), production(1, G1)])
        result = bind_players(players, data, allowed_unit_guids={10: {G1}, 20: {G1}})
        self.assertTrue(result["validated"])
        self.assertEqual(result["bindings"], {0: 10, 1: 20})

    def test_deck_conflict_is_rejected_instead_of_reassigning_actor(self):
        players = [(10, {"PlayerAlliance": 0}), (20, {"PlayerAlliance": 0})]
        result = bind_players(players, stream(orders=[production(0, G2)]),
                              allowed_unit_guids={10: {G1}, 20: {G2}})
        self.assertFalse(result["validated"])
        self.assertEqual(result["bindings"], {})
        self.assertEqual(result["conflicts"][0]["code"], "production_deck_conflict")

    def test_transport_membership_is_checked(self):
        players = [(0, {"PlayerAlliance": 0})]
        result = bind_players(players, stream([0], [production(0, G1, transport=TRANSPORT)]),
                              allowed_unit_guids={0: {G1}})
        self.assertFalse(result["validated"])

    def test_production_requires_membership_input(self):
        result = bind_players([(0, {"PlayerAlliance": 0})], stream([0], [production(0, G1)]))
        self.assertFalse(result["validated"])
        self.assertEqual(result["conflicts"][0]["code"], "production_membership_unavailable")

    def test_missing_numbers_default_but_present_invalid_values_reject(self):
        for field in ("PlayerSlot", "PlayerIsObserver", "PlayerAlliance"):
            for value in (True, False, None, "", "NaN", 1.0, "-1", "1.5", " 1"):
                with self.subTest(field=field, value=value):
                    raw = {"PlayerAlliance": 0, field: value}
                    result = bind_players([(0, raw)], stream([0]))
                    self.assertFalse(result["validated"])

    def test_duplicate_slot_and_unknown_source_reject(self):
        result = bind_players([(0, {"PlayerAlliance": 0}), (0, {"PlayerAlliance": 1})], stream())
        self.assertFalse(result["validated"])
        result = bind_players([(4, {"PlayerAlliance": 0})], stream([1]))
        self.assertFalse(result["validated"])

    def test_partial_stream_rejects_binding(self):
        data = stream([0])
        data["incomplete_order_packet_count"] = 1
        result = bind_players([(0, {"PlayerAlliance": 0})], data)
        self.assertFalse(result["validated"])

    def test_owner_cannot_be_shared_by_two_sources(self):
        players = [(0, {"PlayerAlliance": 0}), (1, {"PlayerAlliance": 1})]
        result = bind_players(players, stream(orders=[production(0, G1, owner=12), production(1, G2, owner=12)]),
                              allowed_unit_guids={0: {G1}, 1: {G2}})
        self.assertFalse(result["validated"])
        self.assertEqual(result["conflicts"][0]["code"], "owner_shared_by_sources")

    def test_cancel_owner_handle_agrees_with_production_actor(self):
        data = stream([0], [production(0, G1, owner=12)])
        data["cancellation_orders"] = [{"action_type": 13, "source_slot": 0, "owner_object_id": 12}]
        self.assertTrue(bind_players([(0, {"PlayerAlliance": 0})], data, allowed_unit_guids={0: {G1}})["validated"])
        data["cancellation_orders"][0]["owner_object_id"] = 13
        self.assertFalse(bind_players([(0, {"PlayerAlliance": 0})], data, allowed_unit_guids={0: {G1}})["validated"])

    def test_explicit_ace_alias_and_transport_expand_allowed_set(self):
        units = {
            G1: {"catalog_id": "1", "descriptor": "Unit_Base"},
            TRANSPORT: {"catalog_id": "3", "descriptor": "Unit_Truck"},
            ACE: {"catalog_id": "4", "descriptor": "Unit_Hero", "ace_unit_type_descriptor": "Unit_Base"},
        }
        players = [(0, {"PlayerDeckContent": "private-code"})]
        def decoder(code):
            return {"entries": [{"unit_id": 4, "transport_id": 3}]}
        self.assertEqual(allowed_deck_guids(players, {"units": units}, decoder), {0: {ACE, G1, TRANSPORT}})
        units[ACE].pop("ace_unit_type_descriptor")
        self.assertEqual(allowed_deck_guids(players, {"units": units}, decoder), {0: {ACE, TRANSPORT}})


if __name__ == "__main__":
    unittest.main()
