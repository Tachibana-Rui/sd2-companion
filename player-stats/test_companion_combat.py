"""V3 task reports: exact binding, immutable events and independent valuation."""

import contextlib
import copy
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from companion.aggregation import CombatReader, CombatStore, _finish, _metric, _summarize, derive_match, ratio_value
from companion.pricing import Catalog
from companion.receiver import API_PATH, COMBAT_PATH, TASK_SCHEMA, ArchiveIndex, CompositeArchiveIndex, ReceiverError, ReceiverStore, create_server, validate_payload


GUN, TANK, TRUCK, VARIANT = "1" * 32, "2" * 32, "3" * 32, "4" * 32
SHA = "c" * 64


def catalog_file(path):
    units = {GUN: {"name": "Lorraine", "descriptor": "Unit_Lorraine", "catalog_id": "1", "cost": 130, "category": "artillery"},
             TANK: {"name": "Tank", "descriptor": "Unit_Tank", "catalog_id": "2", "cost": 200, "category": "tank"},
             TRUCK: {"name": "Truck", "descriptor": "Unit_Truck", "catalog_id": "3", "cost": 15, "category": "support"},
             VARIANT: {"name": "Tank", "descriptor": "Unit_Tank_Other", "catalog_id": "4", "cost": 250, "category": "tank"}}
    path.write_text(json.dumps({"schema": 1, "game_build": "168061", "catalog_sha256": "d" * 64,
                                "source_hashes": {"UniteDescriptor.ndf": "f" * 64}, "units": units}), encoding="utf-8")
    return Catalog(path)


def actor(guid=GUN, owner="123", name="One", unit="Lorraine"):
    return {"player_name": name, "unit_name": unit, "soldier_name": "Crew",
            "unit_label_texture_token": 12345, "xp_texture_token": 836, "display_veterancy": 0,
            "player_label_color_token": 2, "is_ace": False, "unit_guid": guid,
            "player_user_id": owner, "identity_status": "resolved_native_stats" if guid else "unresolved"}


def payload(submission="a" * 64, *, replay=True):
    players = [{"row_index": i, "name": name, "player_user_id": owner, "alliance": i,
                "alliance_display_value": i, "official_kill_points": k, "official_loss_points": loss}
               for i, (name, owner, k, loss) in enumerate((("One", "123", 1234, 321), ("Two", "456", 321, 1234)))]
    gun, tank = actor(), actor(TANK, "456", "Two", "Tank")
    rows = [{"row_index": i, "game_time_seconds": t, "time_display": "00:01", "killer": copy.deepcopy(k), "victim": copy.deepcopy(v)}
            for i, (t, k, v) in enumerate(((1.0, gun, tank), (1.0, gun, tank), (2.0, tank, gun)))]
    result = {"schema": TASK_SCHEMA, "submission_id": submission, "installation_id": str(uuid.UUID(int=1)),
              "source": "player_client_official_task_report", "binding_mode": "replay_sha256" if replay else "native_session",
              "session_id": "session-one", "game_build": 168061, "map": "map-one", "native_players": players,
              "rows": rows, "exe_sha256": "b" * 64, "report_generation": 1,
              "report_data": {"identity_source": "native_stats_history", "identity_status": "complete", "matched_rows": 3, "total_rows": 3}}
    if replay:
        result["client_replay_sha256"] = SHA
        result["client_context_evidence"] = {"association": "observed_game_file_read", "file_body_io_observed": True}
    else:
        result["client_context_evidence"] = {"association": "native_game_session", "session_origin": "native_game_setup",
            "native_source_kind": "network_live", "native_game_ended": True, "observed_running": True,
            "report_observed_after_end": True, "context_epoch": "e" * 64}
    return result


def production(guid=GUN, count=8, role="unit"):
    return {"guid": guid, "role": role, "name": "catalog unit", "requested_count": count + 1,
            "cancelled_count": 1, "net_count": count, "catalog_cost": 99999}


def archive_document(sha=SHA, session="session-one", *, count=8):
    players = []
    for owner, alliance, units in (("123", 0, [production(count=count)]), ("456", 1, [production(TANK, 3)])):
        players.append({"id": owner, "alliance": alliance, "is_observer": False, "participation_verified": True,
            "stream_source_slot": alliance, "slot_binding": {"status": "cross_validated"},
            "stats": {"production_orders": {"units": units, "evidence": "production_requests",
                "cancellation_adjustment": {"status": "approximate", "unmatched_count": 0}}}})
    return {"schema": 1, "source": {"origin": "server_collection", "sha256": sha},
            "match": {"session_id": session, "version": 168061, "map": "map-one", "completion": {"status": "finished"}},
            "players": players, "catalog": {"sha256": "d" * 64}, "identity_binding": {"validated": True},
            "capabilities": {"production_orders": True}}


def save_archive(path, document=None, *, room="room-one", at="2026-10-30T00:00:00+00:00"):
    document = document or archive_document()
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS reports(sha256 TEXT PRIMARY KEY,match_key TEXT,payload TEXT,completion_status TEXT,analyzed_at TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS report_sources(sha256 TEXT,server_id TEXT,server_name TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS report_players(sha256 TEXT,player_id TEXT,participated INTEGER,observed_at TEXT)")
        sha = document["source"]["sha256"]
        db.execute("INSERT INTO reports VALUES(?,?,?,?,?)", (sha, document["match"]["session_id"], json.dumps(document), "finished", at))
        db.execute("INSERT INTO report_sources VALUES(?,?,?)", (sha, room, room))
        for player in document["players"]:
            db.execute("INSERT INTO report_players VALUES(?,?,1,?)", (sha, player["id"], at))


class CombatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sd2-combat-v3-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.catalog = catalog_file(self.base / "catalog.json")
        self.archive = self.base / "archive.sqlite3"
        self.inbox = self.base / "inbox.sqlite3"
        self.combat = self.base / "combat.sqlite3"

    def setup_stores(self):
        save_archive(self.archive)
        return ReceiverStore(self.inbox, archive_index=ArchiveIndex(self.archive)), CombatStore(self.combat, catalog=self.catalog)

    def test_default_catalog_reads_original_numeric_string_build_without_rewriting(self):
        catalog = Catalog()
        self.assertEqual(catalog.game_build, 168061)
        self.assertEqual(len(catalog.units), 3885)
        self.assertEqual(catalog.unit("c6731ffa05d64c55bc7eb4307e77764e", 168061)["cost"], 130)
        self.assertEqual({catalog.units[g]["cost"] for g in ("ca4872c0725b44c590749a95869ab99a", "f87849cbbd5941ae801db5c20c5a7d04")}, {110, 125})

    def test_only_v3_with_required_official_points_and_new_fields_is_accepted(self):
        validate_payload(payload())
        validate_payload(payload(replay=False))
        bad = []
        for key in ("exe_sha256", "report_generation", "report_data"):
            value = payload(); del value[key]; bad.append(value)
        for schema in ("sd2-player-task-timeline-v1", "sd2-live-task-timeline-v2", "sd2-replay-task-timeline-v1"):
            value = payload(); value["schema"] = schema; bad.append(value)
        for key, value in (("client_header_sha256", "f" * 64), ("participants", []), ("match_binding", "old"), ("report_generation", 0)):
            item = payload(); item[key] = value; bad.append(item)
        item = payload(); del item["native_players"][0]["official_kill_points"]; bad.append(item)
        item = payload(); item["native_players"][0]["official_loss_points"] = True; bad.append(item)
        item = payload(); item["rows"][0]["killer"]["display_value"] = 130; bad.append(item)
        for item in bad:
            with self.subTest(keys=list(item)), self.assertRaises(ReceiverError):
                validate_payload(item)

    def test_replay_requires_body_io_and_native_never_uses_client_hash(self):
        for edit in (lambda p: p.pop("client_replay_sha256"),
                     lambda p: p["client_context_evidence"].update(file_body_io_observed=False)):
            item = payload(); edit(item)
            with self.assertRaises(ReceiverError): validate_payload(item)
        item = payload(replay=False); item["client_replay_sha256"] = None
        with self.assertRaises(ReceiverError): validate_payload(item)

    def test_actor_identity_cannot_claim_unresolved_guid_or_another_player(self):
        for edit in (lambda a: a.update(identity_status="unresolved"), lambda a: a.update(player_user_id="456"),
                     lambda a: a.update(unit_guid=None)):
            item = payload(); edit(item["rows"][0]["killer"])
            with self.assertRaises(ReceiverError): validate_payload(item)

    def test_identity_counts_are_exact_and_one_sided_resolution_is_valid_partial(self):
        report = payload()
        for row in report["rows"]:
            row["victim"].update(unit_guid=None, identity_status="unresolved")
        report["report_data"].update(identity_status="partial", matched_rows=0)
        validate_payload(report)
        report["report_data"]["identity_status"] = "complete"
        with self.assertRaises(ReceiverError): validate_payload(report)
        report["report_data"].update(identity_status="partial", matched_rows=1)
        with self.assertRaises(ReceiverError): validate_payload(report)
        for row in report["rows"]:
            row["killer"].update(unit_guid=None, identity_status="unresolved")
        report["report_data"].update(identity_status="unavailable", matched_rows=0)
        validate_payload(report)  # Stable history exists but no event matches.

    def test_names_only_same_price_inference_is_separate_from_native_guid(self):
        unresolved = actor(None)
        result = self.catalog.actor(unresolved, 168061)
        self.assertEqual(result["cost"], 130)
        self.assertIsNone(result["guid"])
        self.assertTrue(result["price_inferred"])
        self.assertEqual(result["price_status"], "inferred_common_name_price")
        ambiguous = actor(None, unit="Tank")
        self.assertIsNone(self.catalog.actor(ambiguous, 168061)["cost"])
        self.assertIsNone(self.catalog.actor(actor(), 1)["cost"])
        self.assertIsNone(self.catalog.actor(actor("9" * 32), 168061)["cost"])

    def test_official_scores_and_timeline_estimate_are_separate_and_duplicates_preserved(self):
        result = derive_match(payload(), archive_document(), self.catalog)
        one = result["players"][0]
        self.assertEqual(one["official_kill_points"], 1234)
        self.assertEqual(one["economy"]["P_est"], 1040)
        self.assertEqual(one["economy"]["K"], 400)
        self.assertEqual(one["economy"]["L"], 130)
        self.assertEqual(one["economy"]["conversion_ratio"], 400 / 1040)
        self.assertEqual(len(result["events"]), 3)
        self.assertEqual(one["economy"]["kill_count"], 2)

    def test_cost_uses_matching_catalog_not_archive_cost_or_tendency_count(self):
        archive = archive_document()
        archive["players"][0]["stats"]["tendency"] = {"calculation": {"denominator": 8}}
        self.assertEqual(derive_match(payload(), archive, self.catalog)["players"][0]["economy"]["P_est"], 1040)
        archive["catalog"]["sha256"] = "0" * 64
        self.assertIsNone(derive_match(payload(), archive, self.catalog)["players"][0]["economy"]["P_est"])

    def test_multi_role_unit_counts_kills_as_main(self):
        # The game has no separate mechanisation class and a transport cannot be
        # purchased alone, so a unit bought in both roles is credited to the
        # main role it was purchased for (e.g. SPW 222 as recon).
        archive = archive_document()
        archive["players"][0]["stats"]["production_orders"]["units"] = [production(TRUCK, 1), production(TRUCK, 2, "transport")]
        report = payload()
        for row in report["rows"]:
            for side in ("killer", "victim"):
                if row[side]["player_user_id"] == "123": row[side].update(unit_guid=TRUCK, unit_name="Truck")
        one = derive_match(report, archive, self.catalog)["players"][0]
        self.assertEqual(one["economy_by_scope"]["all_components"]["P_est"], 45)
        self.assertEqual(one["economy_by_scope"]["all_components"]["K"], 400)
        self.assertEqual(one["economy_by_scope"]["main"]["K"], 400)
        self.assertEqual(one["economy_by_scope"]["transport"]["K"], 0)

    def test_malformed_later_production_cannot_leave_trusted_roles_from_earlier_row(self):
        archive = archive_document()
        archive["players"][0]["stats"]["production_orders"]["units"].append({"role": "invalid"})
        one = derive_match(payload(), archive, self.catalog)["players"][0]
        self.assertIsNone(one["economy"]["P_est"])
        self.assertIsNone(one["economy"]["K"])
        self.assertEqual(one["economy_by_scope"]["all_components"]["K"], 400)
        self.assertEqual(one["production_status"], "unavailable")

    def test_same_price_name_inference_does_not_create_false_zero_on_a_guid_row(self):
        report = payload()
        for row in report["rows"]:
            for side in ("killer", "victim"):
                if row[side]["player_user_id"] == "123":
                    row[side].update(unit_guid=None, identity_status="unresolved")
        one = derive_match(report, archive_document(), self.catalog)["players"][0]
        gun = next(r for r in one["units"] if r["guid"] == GUN and r["role_scope"] == "all_components")
        self.assertIsNone(gun["K"])
        self.assertEqual(one["economy_by_scope"]["all_components"]["K"], 400)
        self.assertGreater(one["economy_by_scope"]["all_components"]["inferred_price_count"], 0)

    def test_unknown_victim_price_does_not_become_zero_or_block_official_score(self):
        report = payload()
        report["rows"][0]["victim"]["unit_guid"] = "9" * 32
        one = derive_match(report, archive_document(), self.catalog)["players"][0]
        self.assertIsNone(one["economy"]["K"])
        self.assertEqual(one["economy"]["K_known_value"], 200)
        self.assertIsNone(one["economy"]["conversion_ratio"])
        self.assertEqual(one["official_kill_points"], 1234)

    def test_nonzero_kills_over_zero_input_and_zero_losses_are_json_safe_infinity(self):
        report = payload(); report["rows"] = report["rows"][:2]
        result = derive_match(report, archive_document(count=0), self.catalog)["players"][0]
        self.assertEqual(result["economy"]["P_est"], 0)
        self.assertEqual(result["economy"]["conversion_ratio"], "+inf")
        self.assertEqual(result["economy"]["exchange_ratio"], "+inf")
        self.assertEqual(result["economy"]["net_exchange_per_input"], "+inf")
        json.dumps(result, allow_nan=False)

    def test_ratio_zero_numerator_zero_denominator_and_unknown_are_distinct(self):
        cases = ((0, 0, None), (100, 0, "+inf"), (-100, 0, "+inf"),
                 (0, 100, 0.0), (100, 40, 2.5), (None, 0, None),
                 (100, None, None), (True, 0, None), (float("inf"), 1, None))
        for numerator, denominator, expected in cases:
            with self.subTest(numerator=numerator, denominator=denominator):
                self.assertEqual(ratio_value(numerator, denominator), expected)
        empty = _finish({**_metric("main"), "P_available": True})
        self.assertIsNone(empty["conversion_ratio"])
        self.assertIsNone(empty["exchange_ratio"])
        self.assertIsNone(empty["net_exchange_per_input"])

    def test_zero_loss_and_zero_input_samples_stay_in_aggregate_totals(self):
        def metric(p, k, loss):
            return _finish({**_metric("main"), "P_available": True,
                            "P_known_value": p, "K_known_value": k, "L_known_value": loss})
        no_loss = metric(0, 200, 0)
        other = metric(100, 100, 100)
        total = _summarize([no_loss, other, metric(0, 0, 0)])
        self.assertEqual(total["conversion_sample_count"], 2)
        self.assertEqual(total["exchange_sample_count"], 2)
        self.assertEqual(total["conversion_sample_K"], 300)
        self.assertEqual(total["conversion_sample_P_est"], 100)
        self.assertEqual(total["conversion_ratio"], 3.0)
        self.assertEqual(total["exchange_ratio"], 3.0)
        self.assertEqual(total["net_exchange_per_input"], 2.0)
        only_infinite = _summarize([no_loss, metric(0, 50, 0)])
        self.assertEqual(only_infinite["conversion_ratio"], "+inf")
        self.assertEqual(only_infinite["exchange_ratio"], "+inf")
        # A net ratio has its own numerator and cannot borrow the K/P sample set.
        self.assertEqual(_summarize([metric(0, 0, 100)])["net_exchange_per_input"], "+inf")

    def test_v7_cache_is_rederived_for_v8_without_rewriting_native_report(self):
        receiver, store = self.setup_stores()
        report = payload()
        report["rows"] = report["rows"][:2]
        report["report_data"].update(matched_rows=2, total_rows=2)
        receiver.submit(report)
        inbox_before = self.inbox.read_bytes()
        with patch("companion.aggregation.MODEL_VERSION", "sd2-combat-economy-v7"):
            self.assertEqual(store.refresh(self.inbox, [self.archive])["derived_matches"], 1)
        with contextlib.closing(sqlite3.connect(self.combat)) as db, db:
            row = db.execute("SELECT payload FROM player_match_combat WHERE player_user_id='123'").fetchone()
            old = json.loads(row[0])
            old["economy"]["exchange_ratio"] = None
            for metrics in old["economy_by_scope"].values():
                metrics["exchange_ratio"] = None
            db.execute("UPDATE player_match_combat SET payload=? WHERE player_user_id='123'", (json.dumps(old),))
        legacy = CombatReader(self.combat).player("123")
        self.assertTrue(legacy["available"])
        self.assertEqual(legacy["economy"]["exchange_ratio"], "+inf")
        self.assertEqual(store.refresh(self.inbox, [self.archive])["derived_matches"], 1)
        current = store.player("123")
        self.assertEqual(current["model_version"], "sd2-combat-economy-v8")
        self.assertEqual(current["matches"][0]["economy"]["exchange_ratio"], "+inf")
        self.assertEqual(current["economy"]["exchange_ratio"], "+inf")
        self.assertEqual(store.refresh(self.inbox, [self.archive])["derived_matches"], 0)
        self.assertEqual(self.inbox.read_bytes(), inbox_before)

    def test_only_accepted_is_derived_and_refresh_is_idempotent_and_read_only(self):
        receiver, store = self.setup_stores()
        report = payload(); before = self.archive.read_bytes()
        self.assertEqual(receiver.submit(report)["status"], "accepted")
        inbox_before = self.inbox.read_bytes()
        self.assertEqual(store.refresh(self.inbox, [self.archive])["derived_matches"], 1)
        self.assertEqual(store.refresh(self.inbox, [self.archive])["derived_matches"], 0)
        self.assertEqual(self.archive.read_bytes(), before)
        self.assertEqual(self.inbox.read_bytes(), inbox_before)
        result = CombatReader(self.combat).player("123")
        self.assertTrue(result["available"])
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["economy"]["P_est"], 1040)
        self.assertEqual(len(store.match(SHA)["events"]), 3)
        with contextlib.closing(sqlite3.connect(self.combat)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM derivation_runs").fetchone()[0], 1)

    def test_pending_match_never_enters_derived_totals(self):
        receiver, store = self.setup_stores()
        report = payload(); report["session_id"] = "not-the-server-session"
        self.assertEqual(receiver.submit(report)["status"], "pending_match")
        self.assertEqual(store.refresh(self.inbox, [self.archive])["derived_matches"], 0)
        self.assertFalse(store.player("123")["available"])

    def test_same_game_many_submissions_select_one_and_preserve_conflicts(self):
        receiver, store = self.setup_stores()
        original = payload()
        receiver.submit(original); store.refresh(self.inbox, [self.archive])
        equal = payload("b" * 64); equal["installation_id"] = str(uuid.UUID(int=2))
        receiver.submit(equal); store.refresh(self.inbox, [self.archive])
        self.assertFalse(store.match(SHA)["selection"]["conflict"])
        different = payload("e" * 64)
        different["native_players"][0]["official_kill_points"] += 1
        receiver.submit(different); store.refresh(self.inbox, [self.archive])
        match = store.match(SHA)
        self.assertTrue(match["selection"]["conflict"])
        self.assertEqual(match["selection"]["submission_id"], original["submission_id"])
        self.assertEqual(store.player("123")["match_count"], 1)
        self.assertEqual(store.player("123")["official_totals"]["kill_points"], 1234)

    def test_multimatch_conversion_uses_only_same_complete_numerator_denominator_samples(self):
        receiver, store = self.setup_stores()
        receiver.submit(payload())
        second_sha = "d" * 64
        document = archive_document(second_sha, "session-two", count=2)
        document["capabilities"]["production_orders"] = False
        save_archive(self.archive, document, at="2026-10-30T01:00:00+00:00")
        second = payload("b" * 64)
        second.update(session_id="session-two", client_replay_sha256=second_sha)
        receiver.submit(second)
        store.refresh(self.inbox, [self.archive])
        result = store.player("123")
        self.assertEqual(result["match_count"], 2)
        self.assertEqual(result["economy"]["conversion_sample_count"], 1)
        self.assertEqual(result["economy"]["conversion_sample_K"], 400)
        self.assertEqual(result["economy"]["conversion_sample_P_est"], 1040)
        self.assertEqual(result["economy"]["conversion_ratio"], 400 / 1040)
        self.assertEqual(result["official_totals"]["sample_count"], 2)

    def test_window_is_latest_archived_games_not_latest_uploaded_reports(self):
        receiver, store = self.setup_stores()
        receiver.submit(payload())
        save_archive(self.archive, archive_document("d" * 64, "session-two"), at="2026-10-30T01:00:00+00:00")
        save_archive(self.archive, archive_document("e" * 64, "session-three"), at="2026-10-30T02:00:00+00:00")
        store.refresh(self.inbox, [self.archive])
        self.assertFalse(store.player("123", limit=2)["available"])
        result = store.player("123", limit=3)
        self.assertEqual(result["archived_window_count"], 3)
        self.assertEqual(result["match_count"], 1)

    def test_catalog_revision_replaces_one_match_and_retains_derivation_provenance(self):
        receiver, store = self.setup_stores()
        receiver.submit(payload()); store.refresh(self.inbox, [self.archive])
        path = self.base / "catalog.json"
        document = json.loads(path.read_text())
        document["units"][GUN]["cost"] = 140
        path.write_text(json.dumps(document))
        updated = Catalog(path)
        store.refresh(self.inbox, [self.archive], catalog=updated)
        self.assertEqual(store.player("123")["economy"]["P_est"], 1120)
        with contextlib.closing(sqlite3.connect(self.combat)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM match_report_selection").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM derivation_runs").fetchone()[0], 2)

    def test_read_only_reader_does_not_create_missing_database(self):
        reader = CombatReader(self.combat)
        self.assertFalse(reader.player("123")["available"])
        self.assertFalse(reader.match(SHA)["available"])
        self.assertFalse(reader.status()["available"])
        self.assertFalse(self.combat.exists())

    def test_storage_alias_is_rejected_without_archive_changes(self):
        save_archive(self.archive)
        before = self.archive.read_bytes()
        with self.assertRaisesRegex(ValueError, "must_be_separate"):
            CombatStore(self.archive, catalog=self.catalog)
        self.assertEqual(self.archive.read_bytes(), before)

    def test_http_v3_raw_and_combat_queries_require_bearer(self):
        receiver, combat = self.setup_stores()
        receiver.submit(payload()); combat.refresh(self.inbox, [self.archive])
        server = create_server(receiver, "fixture-token", combat_store=combat)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path in (API_PATH + "/" + "a" * 64, COMBAT_PATH + "/players/123", COMBAT_PATH + "/matches/" + SHA):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                conn.request("GET", path); reply = conn.getresponse(); reply.read()
                self.assertEqual(reply.status, 401); conn.close()
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                conn.request("GET", path, headers={"Authorization": "Bearer fixture-token"})
                reply = conn.getresponse(); body = json.loads(reply.read())
                self.assertEqual(reply.status, 200)
                self.assertTrue(body.get("available", True)); conn.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(3)

    def test_an_idle_inbox_does_not_reparse_stored_payloads(self):
        receiver, store = self.setup_stores()
        receiver.submit(payload())
        self.assertEqual(store.refresh(self.inbox, [self.archive])["derived_matches"], 1)
        with patch("companion.receiver.validate_payload", wraps=validate_payload) as validate, \
                patch.object(CombatStore, "_index_archives", wraps=CombatStore._index_archives) as index:
            result = store.refresh(self.inbox, [self.archive])
            self.assertEqual(result["derived_matches"], 0)
            self.assertEqual(result["new_sources"], 0)
            # Nothing arrived: no stored report is parsed and no archive is rescanned.
            self.assertEqual(validate.call_count, 0)
            self.assertEqual(index.call_count, 0)

    def test_one_new_archive_report_rereads_only_the_round_it_unlocks(self):
        receiver, store = self.setup_stores()
        receiver.submit(payload())
        store.refresh(self.inbox, [self.archive])
        save_archive(self.archive, archive_document("d" * 64, "session-two"), at="2026-10-30T01:00:00+00:00")
        second = payload("b" * 64)
        second.update(session_id="session-two", client_replay_sha256="d" * 64)
        receiver.submit(second)
        with patch("companion.aggregation.derive_match", wraps=derive_match) as derive:
            self.assertEqual(store.refresh(self.inbox, [self.archive])["derived_matches"], 1)
        self.assertEqual(derive.call_count, 1)
        self.assertEqual(store.player("123")["match_count"], 2)

    def test_an_idle_receiver_cycle_skips_reconcile_until_something_changes(self):
        receiver, combat = self.setup_stores()
        receiver.submit(payload()); combat.refresh(self.inbox, [self.archive])
        server = create_server(receiver, "fixture-token", combat_store=combat)
        server.reconcile_interval = 0
        try:
            with patch.object(receiver, "reconcile", wraps=receiver.reconcile) as reconcile:
                server.service_actions()
                self.assertEqual(reconcile.call_count, 1)
                server.service_actions()
                self.assertEqual(reconcile.call_count, 1)
                save_archive(self.archive, archive_document("d" * 64, "session-two"), at="2026-10-30T01:00:00+00:00")
                server.service_actions()
                self.assertEqual(reconcile.call_count, 2)
                # A new upload also wakes the cycle without waiting for the archive.
                pending = payload("e" * 64)
                pending["session_id"] = "not-the-server-session"
                self.assertEqual(receiver.submit(pending)["status"], "pending_match")
                server.service_actions()
                self.assertEqual(reconcile.call_count, 3)
        finally:
            server.server_close()

    def test_reconcile_acceptance_invalidates_previously_cached_pending_signature(self):
        receiver, combat = self.setup_stores()
        receiver.submit(payload())
        original = receiver.archive_index
        class GatedIndex:
            path = original.path
            native_sessions = original.native_sessions
            ready = False
            def __call__(self, report):
                if report['submission_id'] == 'b' * 64 and not self.ready:
                    return {'pending_reason': 'archive_match_pending'}
                return original(report)
        gate = GatedIndex()
        receiver.archive_index = gate
        second = payload('b' * 64)
        self.assertEqual(receiver.submit(second)['status'], 'pending_match')
        combat.refresh(self.inbox, [self.archive])
        gate.ready = True  # Matcher repair, without archive or inbox edits.
        server = create_server(receiver, 'fixture-token', combat_store=combat)
        server.reconcile_interval = 0
        try:
            server.service_actions()
            self.assertEqual(receiver.report(second['submission_id'])['status'], 'accepted')
            with contextlib.closing(sqlite3.connect(self.combat)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM source_reports').fetchone()[0], 2)
            self.assertEqual(combat.status()['match_count'], 1)
        finally:
            server.server_close()

    def test_arrival_during_derivation_remains_unprocessed_until_next_cycle(self):
        receiver, combat = self.setup_stores()
        receiver.submit(payload())
        original = derive_match
        arrived = False
        def with_arrival(*args, **kwargs):
            nonlocal arrived
            if not arrived:
                arrived = True
                self.assertEqual(receiver.submit(payload('b' * 64))['status'], 'accepted')
            return original(*args, **kwargs)
        server = create_server(receiver, 'fixture-token', combat_store=combat)
        server.reconcile_interval = 0
        try:
            with patch('companion.aggregation.derive_match', side_effect=with_arrival):
                server.service_actions()
            with contextlib.closing(sqlite3.connect(self.combat)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM source_reports').fetchone()[0], 1)
            server.service_actions()
            with contextlib.closing(sqlite3.connect(self.combat)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM source_reports').fetchone()[0], 2)
            self.assertEqual(combat.status()['match_count'], 1)
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
