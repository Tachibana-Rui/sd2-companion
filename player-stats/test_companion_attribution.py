"""Kill attribution: off-map observers, carried transports and multi-role units."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from companion.aggregation import derive_match
from companion.pricing import Catalog
from companion.receiver import TASK_SCHEMA


GUN, TANK, TRUCK, CARRIED, OFFMAP, OBSERVER, OTHER_OBSERVER, BOTH, MULTI = (
    "1" * 32, "2" * 32, "3" * 32, "7" * 32, "6" * 32, "5" * 32, "9" * 32, "8" * 32, "a" * 32)
UNITS = {
    GUN: {"name": "Lorraine", "descriptor": "Unit_Lorraine", "catalog_id": "1", "cost": 130, "category": "artillery"},
    TANK: {"name": "Tank", "descriptor": "Unit_Tank", "catalog_id": "2", "cost": 200, "category": "tank"},
    TRUCK: {"name": "Truck", "descriptor": "Unit_Truck", "catalog_id": "3", "cost": 15, "category": "support"},
    CARRIED: {"name": "Aufklaerer", "descriptor": "Unit_Aufk", "catalog_id": "7", "cost": 15, "category": "recon"},
    OBSERVER: {"name": "Beo 172.5", "descriptor": "Unit_Beo", "catalog_id": "5", "cost": 165, "category": "artillery"},
    OTHER_OBSERVER: {"name": "Beo 172.5 II", "descriptor": "Unit_Beo_II", "catalog_id": "9", "cost": 160,
                     "category": "artillery"},
    BOTH: {"name": "SPW 222", "descriptor": "Unit_Spw222", "catalog_id": "8", "cost": 40, "category": "recon"},
    MULTI: {"name": "Schwimmwagen", "descriptor": "Unit_Schwimm", "catalog_id": "10", "cost": 5, "category": "recon"},
}


def write_catalog(directory, units=None, links=None):
    units = units or UNITS
    path = directory / "unit-guid-map.json"
    path.write_text(json.dumps({"schema": 1, "game_build": "168061", "catalog_sha256": "d" * 64,
                                "source_hashes": {"UniteDescriptor.ndf": "f" * 64}, "units": units}),
                    encoding="utf-8")
    if links:
        raw = path.read_bytes()
        (directory / "offmap-map.json").write_text(json.dumps({
            "schema": 1, "game_build": 168061, "unit_guid_map_sha256": hashlib.sha256(raw).hexdigest(),
            "offmap_units": {guid: {"descriptor": "Unit_Offmap", "type_unit": "ArtHowit"} for guid in links},
            "links": links}), encoding="utf-8")
    return Catalog(path)


def actor(guid, owner, unit_name):
    return {"player_name": "P" + owner, "unit_name": unit_name, "soldier_name": "Crew",
            "unit_label_texture_token": 1, "xp_texture_token": 2, "display_veterancy": 0,
            "player_label_color_token": 3, "is_ace": False, "unit_guid": guid,
            "player_user_id": owner, "identity_status": "resolved_native_stats"}


def order(guid, role, count):
    return {"guid": guid, "role": role, "name": "catalog unit", "requested_count": count,
            "cancelled_count": 0, "net_count": count, "catalog_cost": 99999}


def archive(units_one, *, raw_orders=()):
    players = [{"id": "123", "alliance": 0, "is_observer": False, "participation_verified": True,
                "stream_source_slot": 0, "slot_binding": {"status": "cross_validated"},
                "stats": {"production_orders": {"units": units_one, "evidence": "production_requests",
                          "cancellation_adjustment": {"status": "approximate", "unmatched_count": 0}}}},
               {"id": "456", "alliance": 1, "is_observer": False, "participation_verified": True,
                "stream_source_slot": 1, "slot_binding": {"status": "cross_validated"},
                "stats": {"production_orders": {"units": [order(TANK, "unit", 3)], "evidence": "production_requests",
                          "cancellation_adjustment": {"status": "approximate", "unmatched_count": 0}}}}]
    return {"schema": 1, "source": {"origin": "server_collection", "sha256": "c" * 64},
            "match": {"session_id": "session-one", "version": 168061, "map": "map-one",
                      "completion": {"status": "finished"}},
            "players": players, "catalog": {"sha256": "d" * 64}, "identity_binding": {"validated": True},
            "capabilities": {"production_orders": True},
            "format": {"command_stream": {"production_orders": list(raw_orders)}}}


def payload(rows):
    players = [{"row_index": 0, "name": "P123", "player_user_id": "123", "alliance": 0,
                "alliance_display_value": 0, "official_kill_points": 600, "official_loss_points": 0},
               {"row_index": 1, "name": "P456", "player_user_id": "456", "alliance": 1,
                "alliance_display_value": 1, "official_kill_points": 0, "official_loss_points": 600}]
    return {"schema": TASK_SCHEMA, "submission_id": "a" * 64, "installation_id": "b" * 24,
            "source": "player_client_official_task_report", "binding_mode": "native_session",
            "session_id": "session-one", "game_build": 168061, "map": "map-one",
            "native_players": players, "exe_sha256": "b" * 64, "report_generation": 1,
            "report_data": {"identity_source": "native_stats_history", "identity_status": "complete",
                            "matched_rows": len(rows), "total_rows": len(rows)},
            "rows": rows}


def row(index, seconds, killer, victim):
    return {"row_index": index, "game_time_seconds": seconds, "time_display": "00:01",
            "killer": killer, "victim": victim}


def metric(result, player, guid=None, scope="main"):
    for entry in result["players"]:
        if entry["player_user_id"] != player:
            continue
        rows = entry["units"] if guid is not None else entry["economy_scopes"]
        for item in rows:
            if guid is None:
                if item["role_scope"] == scope:
                    return item
            elif item.get("guid") == guid and item["role_scope"] == scope:
                return item
    raise AssertionError("metric not found")


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sd2-attribution-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.catalog = write_catalog(self.base, links={OFFMAP: [OBSERVER, OTHER_OBSERVER]})
        self.units_one = [order(OBSERVER, "unit", 2), order(CARRIED, "unit", 3),
                          order(TRUCK, "transport", 3), order(BOTH, "unit", 2),
                          order(BOTH, "transport", 2), order(MULTI, "transport", 2)]
        self.raw_orders = [
            {"source_slot": 0, "unit_guids": [CARRIED], "transport_guid": TRUCK},
            {"source_slot": 0, "unit_guids": [BOTH], "transport_guid": BOTH},
            {"source_slot": 0, "unit_guids": [CARRIED, OBSERVER], "transport_guid": MULTI},
        ]

    def derive(self, rows):
        return derive_match(payload(rows), archive(self.units_one, raw_orders=self.raw_orders), self.catalog)

    def test_off_map_strike_belongs_to_the_purchased_observer(self):
        result = self.derive([row(0, 1.0, actor(OFFMAP, "123", "Off-map battery"),
                                  actor(TANK, "456", "Tank"))])
        observer = metric(result, "123", OBSERVER)
        self.assertEqual(observer["kill_count"], 1)
        self.assertEqual(observer["K"], 200)
        self.assertEqual(observer["offmap_observer_kill_count"], 1)
        self.assertEqual(observer["unknown_kill_scope_count"], 0)
        units = next(p for p in result["players"] if p["player_user_id"] == "123")["units"]
        self.assertFalse(any(item.get("guid") == OFFMAP for item in units))
        self.assertEqual(metric(result, "123")["K"], 200)

    def test_two_candidate_observers_stay_unattributed(self):
        catalog = write_catalog(self.base, links={OFFMAP: [OBSERVER, OTHER_OBSERVER]})
        units_one = [order(OBSERVER, "unit", 2), order(OTHER_OBSERVER, "unit", 1)]
        result = derive_match(payload([row(0, 1.0, actor(OFFMAP, "123", "Off-map battery"),
                                           actor(TANK, "456", "Tank"))]),
                                       archive(units_one), catalog)
        economy = metric(result, "123")
        self.assertEqual(economy["unknown_kill_scope_count"], 1)
        self.assertIsNone(economy["K"])

    def test_transport_kill_record_stays_on_the_vehicle_with_carried_classification(self):
        result = self.derive([row(0, 1.0, actor(TRUCK, "123", "Truck"), actor(TANK, "456", "Tank"))])
        truck = metric(result, "123", TRUCK, scope="transport")
        self.assertEqual(truck["kill_count"], 1)
        self.assertEqual(truck["K"], 200)
        self.assertEqual(truck["category"], "support")  # the vehicle keeps its identity
        self.assertEqual(truck["carried_category_kill_count"], 1)
        # ... but the kill is classified under the carried unit's arm.
        recon = next(item for item in result["players"][0]["categories"]
                     if item["category"] == "recon" and item["role_scope"] == "transport")
        self.assertEqual(recon["K"], 200)
        self.assertEqual(metric(result, "123", CARRIED)["kill_count"], 0)

    def test_multi_role_unit_prefers_the_purchased_main_role(self):
        result = self.derive([row(0, 1.0, actor(BOTH, "123", "SPW 222"), actor(TANK, "456", "Tank"))])
        both = metric(result, "123", BOTH)
        self.assertEqual(both["kill_count"], 1)
        self.assertEqual(both["K"], 200)
        self.assertEqual(both["category"], "recon")
        self.assertEqual(both["unknown_kill_scope_count"], 0)

    def test_unproduced_unit_flags_the_match_as_ai_controlled(self):
        # Nothing in this player's production lists the Lorraine, yet it fights:
        # that is AI production after a disconnect (or a captured unit when the
        # replay-side input evidence is no longer available).
        result = self.derive([row(0, 1.0, actor(GUN, "123", "Lorraine"), actor(TANK, "456", "Tank"))])
        me = next(p for p in result["players"] if p["player_user_id"] == "123")
        self.assertFalse(me["applicable"])
        self.assertEqual(me["not_applicable_reason"], "ai_production_detected")
        self.assertEqual(me["unpurchased_events"], {"kill": 1, "loss": 0})
        unit = metric(result, "123", GUN)
        self.assertEqual(unit["kill_count"], 1)
        self.assertEqual(unit["K"], 200)
        self.assertEqual(unit["unpurchased_kill_count"], 1)
        self.assertFalse(unit["purchased"])
        other = next(p for p in result["players"] if p["player_user_id"] == "456")
        self.assertTrue(other["applicable"])

    def test_transport_bound_to_two_passengers_keeps_own_category(self):
        result = self.derive([row(0, 1.0, actor(MULTI, "123", "Schwimmwagen"), actor(TANK, "456", "Tank"))])
        vehicle = metric(result, "123", MULTI, scope="transport")
        self.assertEqual(vehicle["kill_count"], 1)
        self.assertEqual(vehicle["category"], "recon")
        self.assertEqual(vehicle["category_unresolved_kill_count"], 1)

    def test_mutual_destruction_counts_both_kill_and_loss(self):
        # Two units kill each other in the same second; each event supplies one
        # kill for the attacker and one loss for the victim, so both are counted.
        result = self.derive([row(0, 1.0, actor(OBSERVER, "123", "Beo"), actor(TANK, "456", "Tank")),
                              row(1, 1.0, actor(TANK, "456", "Tank"), actor(OBSERVER, "123", "Beo"))])
        one = next(p for p in result["players"] if p["player_user_id"] == "123")
        two = next(p for p in result["players"] if p["player_user_id"] == "456")
        self.assertEqual((one["economy"]["K"], one["economy"]["L"]), (200, 165))
        self.assertEqual((two["economy"]["K"], two["economy"]["L"]), (165, 200))

    def test_official_unit_counts_reveal_rows_missing_from_the_timeline(self):
        # The official aggregate counted one more enemy kill than the timeline
        # shows; that is the documented "no valid killer -> history skipped" gap.
        report = payload([row(0, 1.0, actor(OBSERVER, "123", "Beo"), actor(TANK, "456", "Tank"))])
        report["native_players"][0].update(official_kill_unit_count=2, official_loss_unit_count=0)
        result = derive_match(report, archive(self.units_one, raw_orders=self.raw_orders), self.catalog)
        me = next(p for p in result["players"] if p["player_user_id"] == "123")
        self.assertEqual(me["unit_count_check"]["official_kill_units"], 2)
        self.assertEqual(me["unit_count_check"]["timeline_kill_rows"], 1)
        self.assertEqual(me["unit_count_check"]["missing_kill_units"], 1)
        other = next(p for p in result["players"] if p["player_user_id"] == "456")
        self.assertIsNone(other["unit_count_check"]["official_kill_units"])
        self.assertIsNone(other["unit_count_check"]["missing_kill_units"])


        # A player may just idle for a minute: a quiet stretch is informational
        # only. The decided evidence is an unpurchased unit that fights.
        document = archive(self.units_one, raw_orders=self.raw_orders)
        document["players"][0]["control"] = {"status": "ai_takeover_suspected", "gap_seconds": 812.5}
        result = derive_match(payload([row(0, 1.0, actor(OBSERVER, "123", "Beo"),
                                           actor(TANK, "456", "Tank"))]), document, self.catalog)
        player = next(p for p in result["players"] if p["player_user_id"] == "123")
        self.assertTrue(player["applicable"])
        self.assertIsNone(player["not_applicable_reason"])
        self.assertEqual(player["control"]["status"], "ai_takeover_suspected")


if __name__ == "__main__":
    unittest.main()

