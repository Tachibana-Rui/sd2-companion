"""Public v3 report projection preserves native events without leaking pointers."""
import copy
import json
import unittest

from companion.service import public_report_data, report_quality
from test_companion_replay_capture import native_report


class PublicReportTests(unittest.TestCase):
    def report(self):
        row = {"row_index": 0, "game_time_seconds": 1.0000000001,
               "killer": {"player_name": "Player", "unit_name": "Tank"},
               "victim": {"player_name": "Player", "unit_name": "AT"}}
        report = native_report(rows=[row, {**row, "row_index": 1}])
        report["metadata"]["report_data"]["private_address"] = 0x123456
        report["metadata"]["private_secret"] = "do-not-export"
        return report

    def test_public_projection_preserves_duplicate_native_events_and_exact_float(self):
        report = self.report()
        before = copy.deepcopy(report)
        value = public_report_data(report)
        self.assertIs(value["rows"], report["rows"])
        self.assertIs(value["native_players"], report["players"])
        self.assertEqual(value["rows"], before["rows"])
        self.assertEqual(len(value["rows"]), 2)
        self.assertEqual(value["rows"][0]["game_time_seconds"], 1.0000000001)
        self.assertEqual(value["report_generation"], report["generation"])
        self.assertEqual(value["exe_sha256"], report["metadata"]["exe_sha256"])
        self.assertEqual(report, before)
        encoded = json.dumps(value)
        for private in ("variant_address", "result_address", "private_address", "private_secret", "do-not-export"):
            self.assertNotIn(private, encoded)
        self.assertEqual(set(value["report_data"]),
                         {"identity_source", "identity_status", "matched_rows", "total_rows"})

    def test_executable_fingerprint_is_required_instead_of_filled_from_another_build(self):
        for invalid in (None, "", "a" * 63, "A" * 64, {"address": 1}):
            with self.subTest(invalid=invalid):
                report = self.report()
                report["metadata"]["exe_sha256"] = invalid
                with self.assertRaisesRegex(ValueError, "invalid_report_executable_hash"):
                    public_report_data(report)

    def test_report_metadata_rejects_arbitrary_or_inconsistent_values(self):
        for key, value in (("identity_source", {"address": 123}), ("identity_status", "secret"),
                           ("matched_rows", True), ("matched_rows", 3), ("total_rows", 1)):
            with self.subTest(key=key, value=value):
                report = self.report()
                report["metadata"]["report_data"][key] = value
                with self.assertRaisesRegex(ValueError, "invalid_report_data"):
                    public_report_data(report)

    def test_quality_counts_zero_official_scores_and_only_verified_native_unit_identities(self):
        report = self.report()
        report["players"][0].update(official_kill_points=0, official_loss_points=0)
        report["players"].append({"official_kill_points": 1, "official_loss_points": None})
        actors = [row[role] for row in report["rows"] for role in ("killer", "victim")]
        actors[0].update(unit_guid="a" * 32, player_user_id=None, identity_status="resolved_native_stats")
        actors[1].update(unit_guid="b" * 32, player_user_id="123", identity_status="ambiguous")
        actors[2].update(unit_guid=None, player_user_id="123", identity_status="unresolved")
        actors[3].update(unit_guid="not-a-guid", identity_status="resolved_native_stats")
        self.assertEqual(report_quality(report), {"official_score_players": 1, "total_players": 2,
                                                  "resolved_actors": 1, "total_actors": 4})
        self.assertEqual(report_quality(public_report_data(report)), report_quality(report))


if __name__ == "__main__":
    unittest.main()
