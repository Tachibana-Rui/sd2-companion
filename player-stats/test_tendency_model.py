import math
import unittest
from unittest.mock import patch

from tendency_model import ALL_RADAR_IDS, MAIN_IDS, MODEL_VERSION, RADAR_IDS, aggregate_tendencies, estimate_tendency


def row(category, count, cost=None, role="unit", guid=None):
    result = {"category": category, "requested_count": count, "catalog_cost": cost, "role": role}
    if guid is not None:
        result["guid"] = guid
    return result


def summary(*rows, compatible=True, cancellations=0, mechanization=None):
    return {
        "units": list(rows), "mapping_compatible": compatible,
        "mapping_version": 168061, "cancellation_request_count": cancellations,
        "mechanization": mechanization,
    }


def transports(paid=0, free=0, supply=0, other=0, unknown=0, value=0):
    eligible = paid + free + other
    return {
        "paid_armed_transport_count": paid, "free_transport_count": free,
        "supply_transport_count": supply, "other_transport_count": other,
        "unknown_transport_count": unknown, "paid_armed_transport_value": value,
        "eligible_transport_count": eligible, "ratio_denominator_count": eligible,
        "ratio": paid / eligible if eligible else None,
    }


class EstimateTests(unittest.TestCase):
    def test_known_catalog_value_weights_main_units_and_excludes_transport(self):
        tendency = estimate_tendency(summary(row("tank", 1, 200), row("infantry", 2, 50), row("transport", 100, 1000, role="transport")))
        self.assertEqual(tendency["status"], "approximate")
        self.assertEqual(tendency["weighting"], "price")
        self.assertAlmostEqual(tendency["shares"]["tank"], 2 / 3)
        self.assertAlmostEqual(tendency["shares"]["infantry"], 1 / 3)
        self.assertEqual(tendency["coverage"], {"known_main_unit_count": 3, "unknown_main_unit_count": 0, "ratio": 1})
        self.assertAlmostEqual(tendency["radar"]["tank"], 100 * math.sqrt(2 / 3))
        self.assertEqual(tendency["used_weights"], {"P": 1, "K": 0, "L": 0})
        self.assertEqual(tendency["exact_metrics"], {"P": None, "K": None, "L": None})

    def test_air_and_recon_remain_in_denominator_without_six_axis_renormalization(self):
        tendency = estimate_tendency(summary(row("air", 8, 100), row("recon", 1, 100), row("tank", 1, 100)))
        self.assertAlmostEqual(tendency["shares"]["tank"], 0.1)
        combat_six = ("infantry", "tank", "anti_tank", "support", "artillery", "anti_air")
        self.assertAlmostEqual(sum(tendency["shares"][key] for key in combat_six), 0.1)
        self.assertAlmostEqual(sum(tendency["shares"][key] for key in MAIN_IDS), 1)
        self.assertEqual(tendency["top_labels"], ["航空"])

    def test_pure_air_is_visible_in_nine_axis_default_and_has_named_tendency(self):
        tendency = estimate_tendency(summary(row("air", 10, 120)))
        self.assertEqual(tendency["shares"]["air"], 1)
        self.assertEqual(tendency["top_labels"], ["航空"])
        self.assertEqual(tendency["radar"]["air"], 100)
        self.assertIsNone(tendency["radar"]["mechanization"])
        self.assertEqual(set(tendency["radar"]), set(ALL_RADAR_IDS))
        self.assertEqual(len(tendency["radar_ids"]), 9)
        self.assertNotIn("defense", tendency["radar_ids"])
        self.assertIn("defense", tendency["radar"])

    def test_one_missing_price_switches_entire_match_to_count(self):
        tendency = estimate_tendency(summary(row("tank", 1, 1000), row("infantry", 3, None)))
        self.assertEqual(tendency["weighting"], "count")
        self.assertEqual(tendency["shares"]["tank"], 0.25)
        self.assertEqual(tendency["shares"]["infantry"], 0.75)

    def test_only_missing_or_invalid_price_uses_count_while_zero_price_stays_valid(self):
        for bad in (-1, float("nan"), float("inf"), True, "100"):
            with self.subTest(bad=bad):
                tendency = estimate_tendency(summary(row("tank", 1, 1000), row("infantry", 1, bad)))
                self.assertEqual(tendency["weighting"], "count")
                self.assertEqual(tendency["shares"]["tank"], 0.5)
        mixed = estimate_tendency(summary(row("tank", 1, 100), row("infantry", 3, 0)))
        self.assertEqual(mixed["weighting"], "price")
        self.assertTrue(mixed["price_complete"])
        self.assertEqual(mixed["shares"]["tank"], 1)
        self.assertEqual(mixed["shares"]["infantry"], 0)
        self.assertEqual(mixed["coverage"]["known_main_unit_count"], 4)

    def test_all_zero_main_value_is_distinct_from_missing_prices(self):
        zero = estimate_tendency(summary(row("tank", 1, 0), row("infantry", 3, 0), mechanization=transports(paid=1, value=20)))
        self.assertEqual(zero["status"], "insufficient")
        self.assertEqual(zero["reason"], "zero_value")
        self.assertEqual(zero["weighting"], "price")
        self.assertTrue(zero["price_complete"])
        self.assertIsNone(zero["shares"])
        self.assertIsNone(zero["radar"])
        self.assertEqual(zero["mechanization"]["ratio"], 1)

    def test_mechanization_is_independent_of_main_class_denominator(self):
        tendency = estimate_tendency(summary(row("infantry", 2, 100), row("tank", 2, 100), mechanization=transports(paid=3, free=1, value=90)))
        self.assertEqual(tendency["shares"]["mechanization"], 0.75)
        self.assertEqual(tendency["radar"]["mechanization"], 100 * math.sqrt(0.75))
        self.assertAlmostEqual(sum(tendency["shares"][key] for key in MAIN_IDS), 1)
        self.assertEqual(tendency["shares"]["tank"], 0.5)
        self.assertEqual(tendency["mechanization"]["ratio_denominator_count"], 4)
        self.assertEqual(tendency["mechanization"]["denominator_label"], "已识别的非补给运输车辆")
        self.assertEqual(tendency["model_version"], MODEL_VERSION)

    def test_supply_and_unknown_vehicles_are_outside_mechanization_denominator(self):
        tendency = estimate_tendency(summary(row("artillery", 1, 100), mechanization=transports(paid=1, free=1, supply=50, unknown=8, value=20)))
        mechanism = tendency["mechanization"]
        self.assertEqual(mechanism["ratio"], 0.5)
        self.assertEqual(mechanism["eligible_transport_count"], 2)
        self.assertEqual(mechanism["supply_transport_count"], 50)
        self.assertEqual(mechanism["unknown_transport_count"], 8)

    def test_free_or_unarmed_non_supply_vehicles_give_observed_zero_not_unknown(self):
        tendency = estimate_tendency(summary(row("infantry", 3, 100), mechanization=transports(free=2, other=1)))
        self.assertEqual(tendency["shares"]["mechanization"], 0)
        self.assertEqual(tendency["radar"]["mechanization"], 0)
        self.assertEqual(tendency["mechanization"]["ratio_valid_match_count"], 1)

    def test_no_eligible_transport_is_null_including_supply_only_matches(self):
        for mechanism in (None, transports(), transports(supply=20), transports(unknown=20)):
            with self.subTest(mechanism=mechanism):
                tendency = estimate_tendency(summary(row("tank", 1, 100), mechanization=mechanism))
                self.assertIsNone(tendency["mechanization"]["ratio"])
                self.assertIsNone(tendency["shares"]["mechanization"])
                self.assertIsNone(tendency["radar"]["mechanization"])
                self.assertEqual(tendency["mechanization"]["ratio_valid_match_count"], 0)

    def test_inconsistent_transport_denominator_is_rejected(self):
        mechanism = transports(paid=1, free=1, supply=10)
        mechanism["eligible_transport_count"] = 12
        with self.assertRaises(ValueError):
            estimate_tendency(summary(row("tank", 1, 100), mechanization=mechanism))

    def test_transport_catalog_metadata_survives_estimate_and_aggregate(self):
        mechanism = transports(paid=1, value=20)
        mechanism.update(catalog_approximation=True, classification_version="transport-v1")
        match = estimate_tendency(summary(row("tank", 1, 100), mechanization=mechanism))
        self.assertTrue(match["catalog_approximation"])
        combined = aggregate_tendencies([match])
        self.assertTrue(combined["mechanization"]["catalog_approximation"])
        self.assertEqual(combined["mechanization"]["classification_version"], "transport-v1")

    def test_unknown_classification_reports_coverage_and_does_not_block_chart(self):
        tendency = estimate_tendency(summary(row("tank", 2, 100), row("unknown", 8), row("unknown", 100, role="transport")))
        self.assertEqual(tendency["status"], "approximate")
        self.assertEqual(tendency["shares"]["tank"], 1)
        self.assertEqual(tendency["coverage"], {"known_main_unit_count": 2, "unknown_main_unit_count": 8, "ratio": 0.2})

    def test_old_build_known_guid_uses_current_catalog_with_explicit_approximation(self):
        catalog = {"game_build": 168061, "units": {"guid-tank": {"category": "tank", "cost": 100}, "guid-air": {"category": "air", "cost": 200}}}
        with patch("tendency_model._current_catalog", return_value=catalog) as reader:
            tendency = estimate_tendency(summary(row("unknown", 2, guid="guid-tank"), row("unknown", 1, guid="guid-air"), compatible=False))
        reader.assert_called_once()
        self.assertEqual(tendency["status"], "approximate")
        self.assertTrue(tendency["catalog_approximation"])
        self.assertEqual(tendency["catalog_version"], 168061)
        self.assertEqual(tendency["shares"]["tank"], 0.5)
        self.assertEqual(tendency["shares"]["air"], 0.5)
        self.assertTrue(any("当前 GUID 目录" in text for text in tendency["limitations"]))

    def test_no_known_units_is_insufficient_and_does_not_invent_zero_radar(self):
        tendency = estimate_tendency(summary(row("unknown", 7)))
        self.assertEqual(tendency["status"], "insufficient")
        self.assertIsNone(tendency["radar"])
        self.assertIsNone(tendency["shares"])
        self.assertEqual(tendency["coverage"]["ratio"], 0)
        empty = estimate_tendency(summary(row("transport", 8, role="transport")))
        self.assertEqual(empty["coverage"]["known_main_unit_count"], 0)
        self.assertIsNone(empty["coverage"]["ratio"])

    def test_cancellation_requests_do_not_subtract_units_or_forge_losses(self):
        tendency = estimate_tendency(summary(row("tank", 3, 100), cancellations=9))
        self.assertEqual(tendency["coverage"]["known_main_unit_count"], 3)
        self.assertEqual(tendency["cancellation_request_count"], 9)
        self.assertIsNone(tendency["kill_value"])
        self.assertIsNone(tendency["loss_value"])
        self.assertFalse(tendency["deployment_verified"])

    def test_uniform_nine_category_mix_is_not_mislabeled_as_first_axis(self):
        tendency = estimate_tendency(summary(*(row(key, 1, 100) for key in MAIN_IDS)))
        self.assertEqual(tendency["top_labels"], ["混合投入"])

    def test_malformed_counts_fail_instead_of_becoming_tendency(self):
        for bad in (True, -1, 0.5, float("nan"), "2"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    estimate_tendency(summary(row("tank", bad, 100)))


class AggregateTests(unittest.TestCase):
    def test_mechanization_averages_only_applicable_matches_instead_of_pooling_vehicles(self):
        small = estimate_tendency(summary(row("tank", 1, 100), mechanization=transports(paid=2, value=40)))
        large = estimate_tendency(summary(row("tank", 1000, 100), mechanization=transports(free=100)))
        absent = estimate_tendency(summary(row("tank", 1, 100), mechanization=transports(supply=300)))
        result = aggregate_tendencies([small, large, absent])
        mechanism = result["mechanization"]
        self.assertEqual(result["match_count"], 3)
        self.assertEqual(mechanism["ratio_valid_match_count"], 2)
        self.assertEqual(mechanism["ratio"], 0.5)
        self.assertAlmostEqual(mechanism["pooled_ratio"], 2 / 102)
        self.assertEqual(mechanism["eligible_transport_count"], 102)
        self.assertEqual(mechanism["supply_transport_count"], 300)
        self.assertEqual(mechanism["paid_armed_transport_value"], 40)
        self.assertEqual(result["shares"]["mechanization"], 0.5)
        self.assertAlmostEqual(sum(result["shares"][key] for key in MAIN_IDS), 1)

    def test_no_applicable_mechanization_match_remains_null(self):
        one = estimate_tendency(summary(row("tank", 1, 100)))
        two = estimate_tendency(summary(row("infantry", 1, 100), mechanization=transports(supply=100)))
        result = aggregate_tendencies([one, two])
        self.assertIsNone(result["shares"]["mechanization"])
        self.assertIsNone(result["radar"]["mechanization"])
        self.assertEqual(result["mechanization"]["ratio_valid_match_count"], 0)

    def test_missing_transport_value_is_not_filled_with_zero(self):
        one = estimate_tendency(summary(row("tank", 1, 100), mechanization=transports(paid=1, value=None)))
        two = estimate_tendency(summary(row("tank", 1, 100), mechanization=transports(paid=1, value=20)))
        result = aggregate_tendencies([one, two])
        self.assertIsNone(result["mechanization"]["paid_armed_transport_value"])
        self.assertEqual(result["mechanization"]["ratio"], 1)

    def test_cached_special_share_does_not_enter_sum_one_and_must_match_metadata(self):
        match = estimate_tendency(summary(row("tank", 1, 100), mechanization=transports(paid=1, free=1, value=20)))
        self.assertEqual(aggregate_tendencies([match])["shares"]["tank"], 1)
        match["shares"]["mechanization"] = 2
        with self.assertRaises(ValueError):
            aggregate_tendencies([match])
    def test_large_match_and_small_match_have_equal_votes(self):
        large = estimate_tendency(summary(row("tank", 1000, 100)))
        small = estimate_tendency(summary(row("infantry", 1, 10)))
        result = aggregate_tendencies([large, small])
        self.assertEqual(result["shares"]["tank"], 0.5)
        self.assertEqual(result["shares"]["infantry"], 0.5)
        self.assertEqual(result["match_count"], 2)
        self.assertEqual(result["coverage"]["known_main_unit_count"], 1001)
        self.assertIn("每局等权", result["method_label"])

    def test_takes_latest_twenty_effective_matches_skipping_insufficient(self):
        tank = estimate_tendency(summary(row("tank", 1, 100)))
        air = estimate_tendency(summary(row("air", 1, 100)))
        no_data = estimate_tendency(summary())
        result = aggregate_tendencies([no_data, *([tank] * 20), air])
        self.assertEqual(result["match_count"], 20)
        self.assertEqual(result["shares"]["tank"], 1)
        self.assertEqual(result["shares"]["air"], 0)
        self.assertIn("样本较充分", result["confidence_label"])

    def test_mixed_weight_bases_remain_explicit_and_are_normalized_per_match(self):
        price = estimate_tendency(summary(row("tank", 1, 200), row("infantry", 1, 100)))
        count = estimate_tendency(summary(row("tank", 1, 200), row("infantry", 1, None)))
        result = aggregate_tendencies([price, count])
        self.assertEqual(result["weighting"], "mixed")
        self.assertAlmostEqual(result["shares"]["tank"], (2 / 3 + 0.5) / 2)

    def test_coverage_counts_and_mean_match_coverage_are_separate(self):
        one = estimate_tendency(summary(row("tank", 1, 100), row("unknown", 1), cancellations=1))
        two = estimate_tendency(summary(row("tank", 9, 100), row("unknown", 1), cancellations=2))
        result = aggregate_tendencies([one, two])
        self.assertEqual(result["coverage"]["ratio"], 10 / 12)
        self.assertAlmostEqual(result["mean_match_coverage_ratio"], 0.7)
        self.assertEqual(result["cancellation_request_count"], 3)
        self.assertEqual(result["used_weights"], {"P": 1, "K": 0, "L": 0})

    def test_empty_aggregate_is_a_complete_insufficient_structure(self):
        result = aggregate_tendencies([])
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["match_count"], 0)
        self.assertEqual(result["radar_ids"], list(RADAR_IDS))
        self.assertIsNone(result["radar"])
        self.assertIsNone(result["kill_value"])

    def test_more_matches_change_sample_label_without_claiming_exact_data(self):
        match = estimate_tendency(summary(row("tank", 1, 100)))
        for count, phrase in ((1, "少量样本"), (4, "少量样本"), (5, "初步倾向"), (10, "多局观察"), (20, "样本较充分")):
            with self.subTest(count=count):
                result = aggregate_tendencies([match] * count)
                self.assertIn(phrase, result["confidence_label"])
                self.assertFalse(result["payment_verified"])
                self.assertIsNone(result["exact_metrics"]["P"])

    def test_invalid_cached_share_values_are_rejected(self):
        match = estimate_tendency(summary(row("tank", 1, 100)))
        match["shares"]["tank"] = float("nan")
        with self.assertRaises(ValueError):
            aggregate_tendencies([match])


class CalculationTests(unittest.TestCase):
    def test_explained_unit_values_match_radar_and_exclude_transport_from_denominator(self):
        result = estimate_tendency(summary(row("infantry", 2, 50), row("tank", 1, 200),
                                           row("transport", 4, 5, role="transport"), row("transport", 2, 0, role="transport")))
        calc = result["calculation"]
        self.assertEqual(calc["main_unit_count"], 3)
        self.assertEqual(calc["main_unit_value"], 300)
        self.assertEqual(calc["transport_unit_count"], 6)
        self.assertEqual(calc["transport_value"], 20)
        self.assertEqual(calc["total_value"], 320)
        self.assertEqual(calc["denominator"], 300)
        tank = next(item for item in calc["categories"] if item["id"] == "tank")
        self.assertEqual(tank["value"], 200)
        self.assertAlmostEqual(tank["share"], 2 / 3)
        self.assertAlmostEqual(tank["radar"], 100 * math.sqrt(2 / 3))
        for unit in calc["units"]:
            self.assertEqual(unit["total_value"], unit["count"] * unit["unit_cost"])
            self.assertEqual(unit["included_in_tendency"], unit["role"] == "unit")

    def test_missing_prices_remain_unknown_while_count_shares_are_explained(self):
        result = estimate_tendency(summary(row("infantry", 2, 50), row("tank", 3, None)))
        calc = result["calculation"]
        self.assertEqual(calc["weighting"], "count")
        self.assertEqual(calc["denominator"], 5)
        self.assertEqual(calc["denominator_unit"], "个")
        self.assertIsNone(calc["main_unit_value"])
        self.assertEqual(calc["known_main_unit_value"], 100)
        self.assertIsNone(calc["total_value"])
        infantry = next(item for item in calc["categories"] if item["id"] == "infantry")
        self.assertEqual(infantry["share"], 0.4)

    def test_unknown_category_is_counted_in_unit_totals_but_not_the_style_denominator(self):
        calc = estimate_tendency(summary(row("tank", 1, 200), row("unknown", 2, 50)))["calculation"]
        self.assertEqual(calc["main_unit_value"], 300)
        self.assertEqual(calc["unknown_unit_count"], 2)
        self.assertEqual(calc["denominator"], 200)
        self.assertFalse(next(item for item in calc["units"] if item["category"] == "unknown")["included_in_tendency"])

    def test_old_catalog_explanation_uses_the_same_effective_names_and_prices(self):
        current = {"game_build": 168061, "units": {
            "tank-guid": {"category": "tank", "cost": 120, "name": "Current tank"},
            "truck-guid": {"category": "support", "cost": 15, "name": "Current truck"}}}
        with patch("tendency_model._current_catalog", return_value=current):
            calc = estimate_tendency(summary(row("unknown", 3, None, guid="tank-guid"),
                                             row("unknown", 2, None, role="transport", guid="truck-guid"), compatible=False))["calculation"]
        self.assertEqual(calc["main_unit_value"], 360)
        self.assertEqual(calc["transport_value"], 30)
        self.assertEqual({unit["name"] for unit in calc["units"]}, {"Current tank", "Current truck"})
        self.assertTrue(all(unit["catalog_approximation"] for unit in calc["units"]))

    def test_zero_value_still_explains_recorded_units_without_fake_shares(self):
        result = estimate_tendency(summary(row("defense", 3, 0)))
        self.assertEqual(result["reason"], "zero_value")
        self.assertEqual(result["calculation"]["main_unit_count"], 3)
        self.assertEqual(result["calculation"]["main_unit_value"], 0)
        self.assertEqual(result["calculation"]["units"][0]["unit_cost"], 0)
        self.assertTrue(all(item["share"] is None for item in result["calculation"]["categories"]))


if __name__ == "__main__":
    unittest.main()
