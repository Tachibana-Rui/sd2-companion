"""Cancellation deductions follow queue groups, ownership, time and evidence."""
import copy
import unittest
from unittest.mock import patch

from production_summary import reconcile_cancellations, summarize_orders
from tendency_model import estimate_tendency


TIMING = {"game_build": "168061", "seconds_by_guid": {"infantry": 3, "tank": 3, "air": 0}}
CATALOG = {"game_build": "168061", "catalog_sha256": "hash", "units": {
    "infantry": {"name": "Infantry", "catalog_id": 1, "category": "infantry", "cost": 30},
    "tank": {"name": "Tank", "catalog_id": 2, "category": "tank", "cost": 100},
    "air": {"name": "Aircraft", "catalog_id": 3, "category": "air", "cost": 120},
    "armed": {"name": "Armed transport", "catalog_id": 4, "category": "infantry", "cost": 20},
    "free": {"name": "Motorized transport", "catalog_id": 5, "category": "infantry", "cost": 0},
}}
TRANSPORT = {"units": {
    "armed": {"metadata_verified": True, "is_transporter": True, "is_supply": False, "is_armed": True},
    "free": {"metadata_verified": True, "is_transporter": True, "is_supply": False, "is_armed": False},
}}


def production(index=0, tick=100, *, source=0, owner=40, factory=7, count=1, battle=False,
               guid="infantry", transport="armed", subtype=1):
    return {"stream_command_index": index, "tick": tick, "source_slot": source, "in_battle": battle,
            "owner_object_id": [owner, 0], "pack_values": [0, factory, 0xFFFFFFFF, 0],
            "requested_count": count, "unit_guids": [guid], "transport_guid": transport,
            "super_transport_guid": None, "subtype": subtype,
            # These per-slot values deliberately differ from production IDs.
            "production_groups": [[1000 + value] for value in range(count)]}


def cancel(index=1, tick=120, *, source=0, owner=40, factory=7, object_id=1, fallback=0, battle=False):
    return {"stream_command_index": index, "tick": tick, "source_slot": source, "in_battle": battle,
            "owner_object_id": owner, "request_values": [factory, object_id, fallback]}


class ProductionCancellationTests(unittest.TestCase):
    def reconcile(self, orders, cancels, **options):
        with patch("production_summary.production_timing", return_value=TIMING):
            return reconcile_cancellations(orders, cancels, tick_rate=10, game_version=168061,
                                          battle_started_tick=1000, **options)

    def summarize(self, orders, cancels):
        adjusted, annotated, method = self.reconcile(orders, cancels)
        with patch("production_summary.guid_map", return_value=CATALOG), \
                patch("production_summary.transport_classification", return_value=TRANSPORT):
            return summarize_orders(adjusted, 168061, "hash", annotated, reconciliation=method)

    def test_one_cancel_removes_one_group_and_both_transport_components(self):
        order = production(count=3)
        order["super_transport_guid"] = "free"
        result = self.summarize([order], [cancel(object_id=2)])
        self.assertEqual(result["requested_main_unit_count"], 3)
        self.assertEqual(result["net_main_unit_count"], 2)
        self.assertEqual(result["cancelled_main_unit_count"], 1)
        self.assertEqual(result["requested_unit_count"], 9)
        self.assertEqual(result["net_unit_count"], 6)
        self.assertEqual(result["mechanization"]["paid_armed_transport_value"], 40)
        self.assertEqual(result["mechanization"]["free_transport_count"], 2)
        self.assertEqual(result["mechanization"]["ratio"], .5)
        tendency = estimate_tendency(result)
        detail = tendency["calculation"]
        self.assertEqual(detail["main_unit_value"], 60)
        self.assertEqual(detail["transport_value"], 40)
        self.assertEqual(detail["total_value"], 100)
        self.assertEqual(detail["requested_main_unit_count"], 3)
        self.assertEqual(detail["cancelled_main_unit_count"], 1)
        self.assertEqual(tendency["exact_metrics"], {"P": None, "K": None, "L": None})
        self.assertFalse(tendency["deployment_verified"])

    def test_radar_uses_remaining_units_and_cancelled_missing_price_does_not_force_count(self):
        orders = [production(guid="infantry"), production(index=1, tick=101, guid="tank", transport=None)]
        result = self.summarize(orders, [cancel(index=2, object_id=1)])
        for row in result["units"]:
            if row["role"] == "unit" and row["net_count"] == 0:
                row["catalog_cost"] = None
        tendency = estimate_tendency(result)
        self.assertEqual(tendency["shares"]["infantry"], 0)
        self.assertEqual(tendency["shares"]["tank"], 1)
        self.assertEqual(tendency["weighting"], "price")

    def test_distinct_cancel_ids_in_a_bulk_pack_do_not_cancel_the_entire_request_twice(self):
        result = self.summarize([production(count=3)], [cancel(object_id=2), cancel(index=2, object_id=3)])
        self.assertEqual(result["net_main_unit_count"], 1)
        self.assertEqual(result["cancelled_main_unit_count"], 2)

    def test_duplicate_cancel_is_ignored_and_raw_evidence_is_not_mutated(self):
        orders = [production(count=2)]
        cancels = [cancel(), cancel(index=2, tick=130)]
        before = copy.deepcopy((orders, cancels))
        adjusted, annotated, _ = self.reconcile(orders, cancels)
        self.assertEqual(adjusted[0]["net_count"], 1)
        self.assertEqual([event["cancellation_status"] for event in annotated], ["matched", "duplicate"])
        self.assertEqual((orders, cancels), before)

    def test_source_owner_and_factory_must_all_match(self):
        for fields in ({"source": 1}, {"owner": 41}, {"factory": 8}):
            with self.subTest(fields=fields):
                adjusted, annotated, _ = self.reconcile([production()], [cancel(**fields)])
                self.assertEqual(adjusted[0]["net_count"], 1)
                self.assertEqual(annotated[0]["cancellation_status"], "unmatched")

    def test_global_sequence_includes_other_players_and_ignores_per_slot_groups(self):
        first = production(source=1, owner=41, count=2)
        second = production(index=1, tick=101)
        adjusted, annotated, _ = self.reconcile([second, first], [cancel(index=2, object_id=3)])
        self.assertEqual([order["net_count"] for order in adjusted], [0, 2])
        self.assertEqual(annotated[0]["cancellation_status"], "matched")
        adjusted, annotated, _ = self.reconcile([production()], [cancel(object_id=1000)])
        self.assertEqual(adjusted[0]["net_count"], 1)
        self.assertEqual(annotated[0]["cancellation_status"], "unmatched")

    def test_cancel_before_production_does_not_remove_a_future_group(self):
        adjusted, annotated, _ = self.reconcile([production(index=1, tick=120)], [cancel(index=0, tick=100)])
        self.assertEqual(adjusted[0]["net_count"], 1)
        self.assertEqual(annotated[0]["cancellation_status"], "unmatched")

    def test_battle_queue_only_deducts_within_verified_guid_duration(self):
        for delta, expected in ((29, "matched"), (30, "expired"), (100, "expired")):
            with self.subTest(delta=delta):
                adjusted, annotated, _ = self.reconcile([production(tick=1100, battle=True, subtype=3)],
                                                       [cancel(tick=1100 + delta, battle=True)])
                self.assertEqual(annotated[0]["cancellation_status"], expected)
                self.assertEqual(adjusted[0]["net_count"], 0 if expected == "matched" else 1)

    def test_zero_duration_aircraft_can_cancel_in_deployment_but_not_after_battle_spawn(self):
        for battle, tick in ((False, 120), (True, 1101)):
            with self.subTest(battle=battle):
                order = production(tick=1100 if battle else 100, battle=battle, subtype=2, guid="air", transport=None)
                adjusted, annotated, _ = self.reconcile([order], [cancel(tick=tick, battle=battle)])
                self.assertEqual(adjusted[0]["net_count"], 1 if battle else 0)
                self.assertEqual(annotated[0]["cancellation_status"], "expired" if battle else "matched")

    def test_deployment_unit_cannot_be_removed_by_a_later_battle_cancel(self):
        adjusted, annotated, _ = self.reconcile([production()], [cancel(tick=1100, battle=True)])
        self.assertEqual(adjusted[0]["net_count"], 1)
        self.assertEqual(annotated[0]["cancellation_status"], "expired")

    def test_unknown_guid_or_fallback_reference_is_not_guessed(self):
        adjusted, annotated, _ = self.reconcile([production(tick=1100, battle=True, guid="unknown")],
                                               [cancel(tick=1101, battle=True)])
        self.assertEqual(adjusted[0]["net_count"], 1)
        self.assertEqual(annotated[0]["cancellation_status"], "unsupported")
        adjusted, annotated, _ = self.reconcile([production()], [cancel(object_id=0, fallback=1000)])
        self.assertEqual(adjusted[0]["net_count"], 1)
        self.assertEqual(annotated[0]["cancellation_status"], "unsupported")

    def test_ai_shift_only_uses_a_unique_still_active_queue_and_duplicate_is_safe(self):
        order = production(tick=1100, battle=True, subtype=3)
        adjusted, annotated, _ = self.reconcile([order], [cancel(tick=1128, battle=True, object_id=999),
                                                        cancel(index=2, tick=1129, battle=True, object_id=999)])
        self.assertEqual(adjusted[0]["net_count"], 0)
        self.assertEqual(annotated[0]["cancellation_match_method"], "unique_active_queue_candidate")
        self.assertEqual(annotated[1]["cancellation_status"], "duplicate")
        adjusted, annotated, _ = self.reconcile([production(tick=1100, battle=True, count=2)],
                                               [cancel(tick=1128, battle=True, object_id=999)])
        self.assertEqual(adjusted[0]["net_count"], 2)
        self.assertEqual(annotated[0]["cancellation_status"], "ambiguous")

    def test_observed_id_drift_disables_later_accidental_sequence_match(self):
        orders = [production(index=0, tick=1100, battle=True),
                  production(index=2, tick=1102, battle=True),
                  production(index=3, tick=1103, battle=True, guid="tank"),
                  production(index=4, tick=1104, battle=True, guid="infantry", transport="free")]
        cancels = [cancel(index=1, tick=1101, battle=True, object_id=3),
                   cancel(index=5, tick=1105, battle=True, object_id=4)]
        adjusted, annotated, metadata = self.reconcile(orders, cancels)
        # A hidden +2 allocator offset makes real ID 4 target the second
        # infantry group, whereas nominal sequence 4 points at another pack.
        self.assertEqual([order["net_count"] for order in adjusted], [0, 1, 1, 1])
        self.assertEqual([event["cancellation_status"] for event in annotated], ["matched", "ambiguous"])
        self.assertTrue(metadata["sequence_drift_detected"])

    def test_partial_stream_duplicate_index_or_reversed_ticks_disables_deduction(self):
        for orders, cancels, options in (([production()], [cancel()], {"stream_complete": False}),
                                       ([production()], [cancel(index=0)], {}),
                                       ([production(tick=120)], [cancel(tick=100)], {})):
            with self.subTest(options=options):
                adjusted, annotated, method = self.reconcile(orders, cancels, **options)
                self.assertEqual(adjusted[0]["net_count"], 1)
                self.assertEqual(annotated[0]["cancellation_status"], "unavailable")
                self.assertEqual(method["status"], "unavailable")

    def test_legacy_air_state_is_corrected_before_global_id_allocation(self):
        orders = [production(subtype=2, guid="air", count=2), production(index=1, tick=101)]
        adjusted, annotated, _ = self.reconcile(orders, [cancel(index=2, object_id=2)])
        self.assertEqual([order["requested_count"] for order in adjusted], [1, 1])
        self.assertEqual([order["net_count"] for order in adjusted], [1, 0])
        self.assertEqual(annotated[0]["cancellation_status"], "matched")

    def test_net_counts_cannot_disagree_with_original_minus_cancelled(self):
        with self.assertRaises(ValueError):
            estimate_tendency({"mapping_compatible": True, "units": [{"role": "unit", "category": "tank",
                              "requested_count": 1, "cancelled_count": 2, "net_count": 0, "catalog_cost": 100}]})

    def test_old_build_timing_is_explicitly_approximate_and_unknown_is_still_retained(self):
        with patch("production_summary.production_timing", return_value=TIMING):
            adjusted, annotated, method = reconcile_cancellations(
                [production(tick=1100, battle=True)], [cancel(tick=1120, battle=True)],
                tick_rate=10, game_version=94419)
        self.assertEqual(adjusted[0]["net_count"], 0)
        self.assertEqual(annotated[0]["cancellation_status"], "matched")
        self.assertTrue(method["timing_approximation"])
        self.assertFalse(method["timing_compatible"])
        self.assertFalse(method["refund_verified"])
        self.assertFalse(method["deployment_verified"])


if __name__ == "__main__":
    unittest.main()
