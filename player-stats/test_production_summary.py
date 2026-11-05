"""Transport prices and supply roles remain distinct in mechanization stats."""
import unittest
from unittest.mock import patch

from production_summary import summarize_mechanization, summarize_orders


def transport(guid, quantity, cost):
    return {"guid": guid, "role": "transport", "requested_count": quantity, "catalog_cost": cost}


META = {"units": {
    "free": {"metadata_verified": True, "is_transporter": True, "is_supply": False, "is_armed": False, "catalog_cost": 0},
    "armed": {"metadata_verified": True, "is_transporter": True, "is_supply": False, "is_armed": True, "catalog_cost": 20},
    "supply": {"metadata_verified": True, "is_transporter": True, "is_supply": True, "is_armed": True, "catalog_cost": 15},
    "other": {"metadata_verified": True, "is_transporter": True, "is_supply": False, "is_armed": False, "catalog_cost": 5},
}}


class MechanizationSummaryTests(unittest.TestCase):
    def summarize(self, *rows, compatible=True):
        with patch("production_summary.transport_classification", return_value=META):
            return summarize_mechanization({"units": list(rows), "mapping_compatible": compatible})

    def test_zero_price_is_valid_free_transport_not_missing(self):
        result = self.summarize(transport("free", 3, 0), transport("armed", 1, 20))
        self.assertEqual(result["free_transport_count"], 3)
        self.assertEqual(result["unknown_transport_count"], 0)
        self.assertEqual(result["paid_armed_transport_value"], 20)
        self.assertEqual(result["ratio"], 0.25)

    def test_supply_is_excluded_even_with_price_and_a_weapon(self):
        result = self.summarize(transport("supply", 100, 15), transport("armed", 2, 20), transport("free", 2, 0))
        self.assertEqual(result["supply_transport_count"], 100)
        self.assertEqual(result["eligible_transport_count"], 4)
        self.assertEqual(result["paid_armed_transport_count"], 2)
        self.assertEqual(result["paid_armed_transport_value"], 40)
        self.assertEqual(result["ratio"], 0.5)

    def test_paid_unarmed_and_unknown_are_not_mechanized(self):
        result = self.summarize(transport("other", 4, 5), transport("absent", 10, 50), transport("armed", 1, None))
        self.assertEqual(result["other_transport_count"], 4)
        self.assertEqual(result["unknown_transport_count"], 11)
        self.assertEqual(result["paid_armed_transport_count"], 0)
        self.assertEqual(result["ratio"], 0)
        self.assertEqual(self.summarize(transport("supply", 10, 15))["ratio"], None)

    def test_old_catalog_prices_are_explicitly_approximate(self):
        result = self.summarize(transport("armed", 2, None), compatible=False)
        self.assertEqual(result["paid_armed_transport_count"], 2)
        self.assertEqual(result["paid_armed_transport_value"], 40)
        self.assertTrue(result["catalog_approximation"])

    def test_each_transport_component_counted_once_in_bulk_pack(self):
        units = {guid: {"name": guid, "catalog_id": guid, "category": "infantry", "cost": cost}
                 for guid, cost in (("main", 30), ("armed", 20), ("free", 0))}
        order = {"requested_count": 3, "unit_guids": ["main"], "transport_guid": "armed", "super_transport_guid": "free"}
        with patch("production_summary.guid_map", return_value={"game_build": "168061", "catalog_sha256": "hash", "units": units}), \
                patch("production_summary.transport_classification", return_value=META):
            result = summarize_orders([order], 168061, "hash")
        self.assertEqual(result["requested_main_unit_count"], 3)
        self.assertEqual(result["requested_unit_count"], 9)
        self.assertEqual(result["mechanization"]["paid_armed_transport_count"], 3)
        self.assertEqual(result["mechanization"]["free_transport_count"], 3)
        self.assertEqual(result["mechanization"]["paid_armed_transport_value"], 60)
        self.assertEqual(result["mechanization"]["ratio"], 0.5)


if __name__ == "__main__":
    unittest.main()
