"""Idempotent, separately stored combat estimates from accepted task reports.

The archive and inbox are opened read-only. Neither native events nor the
existing production/tendency model are edited by this module.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing, contextmanager
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time

from . import open_readonly_database
from .pricing import Catalog


MODEL_VERSION = "sd2-combat-economy-v7"
SCOPES = ("all_components", "main", "transport")
HASH = re.compile(r"[0-9a-f]{64}\Z")
PLAYER = re.compile(r"(?:0|[1-9][0-9]{0,19})\Z")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _inbox_signature(path):
    """Digest what the inbox would offer; never reads payload blobs.

    ``reconcile`` rewrites ``last_checked_at`` on every pass, so the state is
    keyed on the status counters and the newest arrival only.
    """
    try:
        with _read(path) as db:
            rows = db.execute("SELECT status,COUNT(*) AS n,COALESCE(MAX(received_at),0) AS latest "
                              "FROM submissions GROUP BY status").fetchall()
    except (OSError, sqlite3.Error, ValueError):
        return None
    return _digest([[row["status"], row["n"], row["latest"]] for row in rows])


def archive_signature(paths):
    """Digest the finished reports of every archive; never reads payload blobs.

    ``None`` means the archives cannot be certified unchanged, so callers must
    do the full pass instead of trusting a stored value.
    """
    stamps = []
    for path in paths:
        try:
            with _read(path) as db:
                row = db.execute("SELECT COUNT(*) AS n,COALESCE(MAX(analyzed_at),'') AS latest,"
                                 "COALESCE(MAX(rowid),0) AS newest FROM reports "
                                 "WHERE completion_status='finished'").fetchone()
        except (OSError, sqlite3.Error, ValueError):
            return None
        stamps.append([str(Path(path).resolve()), row["n"], row["latest"], row["newest"]])
    return _digest(stamps)


@contextmanager
def _read(path):
    path = Path(path).resolve()
    db = open_readonly_database(path)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        db.close()


def _metric(scope, **identity):
    return {**identity, "role_scope": scope, "P_known_value": 0, "K_known_value": 0,
            "L_known_value": 0, "requested_count": 0, "cancelled_count": 0, "net_count": 0,
            "kill_count": 0, "loss_count": 0, "unknown_input_count": 0,
            "unknown_kill_value_count": 0, "unknown_loss_value_count": 0,
            "unknown_kill_attribution_count": 0, "unknown_loss_attribution_count": 0,
            "unknown_kill_scope_count": 0, "unknown_loss_scope_count": 0,
            "unknown_role_count": 0, "inferred_price_count": 0,
            "offmap_observer_kill_count": 0, "offmap_observer_loss_count": 0,
            "carried_category_kill_count": 0, "carried_category_loss_count": 0,
            "category_unresolved_kill_count": 0, "category_unresolved_loss_count": 0,
            "unpurchased_kill_count": 0, "unpurchased_loss_count": 0,
            "offmap_ambiguous_kill_count": 0, "offmap_ambiguous_loss_count": 0,
            "attribution": None, "P_available": False}


def _finish(row):
    result = dict(row)
    result["P_est"] = row["P_known_value"] if row["P_available"] and not row["unknown_input_count"] else None
    result["K"] = row["K_known_value"] if not any(row[k] for k in
        ("unknown_kill_value_count", "unknown_kill_attribution_count", "unknown_kill_scope_count")) else None
    result["L"] = row["L_known_value"] if not any(row[k] for k in
        ("unknown_loss_value_count", "unknown_loss_attribution_count", "unknown_loss_scope_count")) else None
    p, k, loss = result["P_est"], result["K"], result["L"]
    result["conversion_ratio"] = k / p if p is not None and p > 0 and k is not None else None
    result["net_exchange_per_input"] = (k - loss) / p if p is not None and p > 0 and k is not None and loss is not None else None
    result["exchange_ratio"] = k / loss if loss is not None and loss > 0 and k is not None else None
    result["conversion_sample_count"] = int(result["conversion_ratio"] is not None)
    result["net_sample_count"] = int(result["net_exchange_per_input"] is not None)
    result["exchange_sample_count"] = int(result["exchange_ratio"] is not None)
    result["status"] = "partial" if p is None or k is None or loss is None else "estimated"
    result["purchased"] = bool(row.get("net_count"))
    result["payment_verified"] = False
    result["deployment_verified"] = False
    return result


def _summarize(rows):
    if not rows:
        return {**_finish(_metric("all_components")), "sample_count": 0,
                "P_est": None, "K": None, "L": None, "conversion_ratio": None,
                "net_exchange_per_input": None, "exchange_ratio": None, "status": "unavailable"}
    additive = ("P_known_value", "K_known_value", "L_known_value", "requested_count", "cancelled_count", "net_count",
                "kill_count", "loss_count", "unknown_input_count", "unknown_kill_value_count", "unknown_loss_value_count",
                "unknown_kill_attribution_count", "unknown_loss_attribution_count", "unknown_kill_scope_count",
                "unknown_loss_scope_count", "unknown_role_count", "inferred_price_count",
                "offmap_observer_kill_count", "offmap_observer_loss_count",
                "carried_category_kill_count", "carried_category_loss_count",
                "category_unresolved_kill_count", "category_unresolved_loss_count",
                "unpurchased_kill_count", "unpurchased_loss_count",
                "offmap_ambiguous_kill_count", "offmap_ambiguous_loss_count")
    result = {key: sum(row.get(key, 0) for row in rows) for key in additive}
    result["role_scope"] = rows[0]["role_scope"]
    result["P_available"] = all(row["P_est"] is not None for row in rows)
    result = _finish(result)
    conversion = [r for r in rows if r["P_est"] is not None and r["P_est"] > 0 and r["K"] is not None]
    net = [r for r in conversion if r["L"] is not None]
    exchange = [r for r in rows if r["K"] is not None and r["L"] is not None and r["L"] > 0]
    result.update(sample_count=len(rows), conversion_sample_count=len(conversion), net_sample_count=len(net),
                  exchange_sample_count=len(exchange), conversion_sample_P_est=sum(r["P_est"] for r in conversion),
                  conversion_sample_K=sum(r["K"] for r in conversion))
    result["conversion_ratio"] = sum(r["K"] for r in conversion) / sum(r["P_est"] for r in conversion) if conversion else None
    result["net_exchange_per_input"] = sum(r["K"] - r["L"] for r in net) / sum(r["P_est"] for r in net) if net else None
    result["exchange_ratio"] = sum(r["K"] for r in exchange) / sum(r["L"] for r in exchange) if exchange else None
    return result


def _content_hash(payload):
    players = sorted(({key: p[key] for key in ("player_user_id", "name", "alliance",
                      "official_kill_points", "official_loss_points")} for p in payload["native_players"]),
                     key=lambda p: (p["player_user_id"] or "", p["name"]))
    rows = []
    for row in payload["rows"]:
        actors = []
        for side in ("killer", "victim"):
            actor = row[side]
            actors.append({key: actor[key] for key in ("player_user_id", "player_name", "unit_guid", "unit_name",
                                                     "soldier_name", "display_veterancy", "is_ace", "identity_status")})
        rows.append(_json([float(row["game_time_seconds"]), *actors]))
    # Sorting compares submissions only. It never deletes duplicate events or
    # changes the selected report's original order and row indexes.
    return _digest({"players": players, "rows": sorted(rows)})


def _production_ready(archive, player, catalog):
    summary = player.get("stats", {}).get("production_orders")
    ready = (archive.get("identity_binding", {}).get("validated") is True
             and archive.get("capabilities", {}).get("production_orders") is True
             and player.get("slot_binding", {}).get("status") == "cross_validated"
             and type(player.get("stream_source_slot")) is int
             and archive.get("catalog", {}).get("sha256") == catalog.catalog_sha256
             and archive.get("match", {}).get("version") == catalog.game_build
             and isinstance(summary, dict) and summary.get("evidence") == "production_requests"
             and isinstance(summary.get("units"), list))
    return bool(ready), summary if isinstance(summary, dict) else {}


def derive_match(payload, archive, catalog):
    """Pure calculation; the caller must first establish an accepted binding."""
    roster = {p["player_user_id"]: p for p in payload["native_players"] if p["player_user_id"] is not None}
    by_name = defaultdict(list)
    for identifier, player in roster.items():
        by_name[player["name"]].append(identifier)
    archived = {p.get("id"): p for p in archive.get("players", [])}
    roles = defaultdict(set)
    production = []
    ready = {}
    summaries = {}
    for identifier in roster:
        ready[identifier], summaries[identifier] = _production_ready(archive, archived.get(identifier, {}), catalog)
        if not ready[identifier]:
            continue
        for raw in summaries[identifier]["units"]:
            if not isinstance(raw, dict) or raw.get("role") not in ("unit", "transport"):
                ready[identifier] = False
                break
            quantities = [raw.get(k) for k in ("requested_count", "cancelled_count", "net_count")]
            if (any(type(q) is not int or not 0 <= q <= 2**32 - 1 for q in quantities)
                    or quantities[1] + quantities[2] != quantities[0]):
                ready[identifier] = False
                break
            role = "main" if raw["role"] == "unit" else "transport"
            price = catalog.unit(raw.get("guid"), payload["game_build"])
            production.append({"player_user_id": identifier, "role": role, **raw, "valuation": price})
            if raw["net_count"] > 0:
                roles[(identifier, raw.get("guid"))].add(role)
    production = [row for row in production if ready[row["player_user_id"]]]
    roles.clear()
    for row in production:
        if row["net_count"] > 0:
            roles[(row["player_user_id"], row.get("guid"))].add("main" if row["role"] == "unit" else "transport")
    # A transport cannot be bought alone, so its kills belong to the main unit it
    # carried in the same order; an off-map strike entity deals the kills of the
    # observer that spawns it. Both relations are explicit records, and an
    # ambiguous relation stays unknown instead of being split by guesswork.
    main_guids = defaultdict(set)
    for (identifier, guid), found_roles in roles.items():
        if "main" in found_roles:
            main_guids[identifier].add(guid)
    carried = defaultdict(set)
    slot_owner = {player.get("stream_source_slot"): player.get("id")
                  for player in archive.get("players", []) if player.get("stream_source_slot") is not None}
    stream = (archive.get("format") or {}).get("command_stream") or {}
    for order in stream.get("production_orders") or []:
        if not isinstance(order, dict):
            continue
        owner = slot_owner.get(order.get("source_slot"))
        passengers = [guid for guid in (order.get("unit_guids") or []) if isinstance(guid, str)]
        if owner is None or not passengers:
            continue
        for key in ("transport_guid", "super_transport_guid"):
            guid = order.get(key)
            if isinstance(guid, str) and guid and guid != "0" * 32:
                carried[(owner, guid)].update(passengers)
    player_rows = {(identifier, scope): _metric(scope, player_user_id=identifier) for identifier in roster for scope in SCOPES}
    unit_rows = {}
    category_rows = {}

    def targets(identifier, valuation, name, scope):
        guid = valuation.get("guid")
        key = guid if guid else "unresolved:" + hashlib.sha256((name or "").encode("utf-8")).hexdigest()
        unit = unit_rows.setdefault((identifier, key, scope), _metric(scope, player_user_id=identifier,
            unit_key=key, guid=guid, name=valuation.get("name") or name, category=valuation.get("category", "unknown"),
            catalog_unit_cost=valuation.get("cost"), catalog_price_status=valuation.get("price_status")))
        category = valuation.get("category", "unknown")
        cat = category_rows.setdefault((identifier, category, scope), _metric(scope, player_user_id=identifier, category=category))
        return (player_rows[(identifier, scope)], unit, cat)

    for row in production:
        identifier = row["player_user_id"]
        role = "main" if row["role"] == "unit" else "transport"
        for scope in ("all_components", role):
            for metric in targets(identifier, row["valuation"], row.get("name"), scope):
                metric["requested_count"] += row["requested_count"]
                metric["cancelled_count"] += row["cancelled_count"]
                metric["net_count"] += row["net_count"]
                if row["valuation"]["cost"] is None:
                    metric["unknown_input_count"] += row["net_count"]
                else:
                    metric["P_known_value"] += row["net_count"] * row["valuation"]["cost"]

    def actor(raw):
        identifier = raw.get("player_user_id")
        if identifier is None and len(by_name[raw["player_name"]]) == 1:
            identifier = by_name[raw["player_name"]][0]
        if identifier not in roster:
            identifier = None
        value = catalog.actor(raw, payload["game_build"])
        guid = value.get("guid")
        role = None
        attribution = "direct" if guid else "unresolved"
        if identifier is not None and guid:
            owned_main = main_guids.get(identifier, ())
            observer = catalog.observer_for(guid, owned_main)
            if observer is not None:
                resolved = catalog.unit(observer, payload["game_build"])
                if resolved.get("guid"):
                    value, role, attribution = resolved, "main", "offmap_observer"
            elif guid in catalog.offmap_units:
                # A known off-map strike entity whose owning observer cannot be
                # determined (several candidates purchased) stays unattributed.
                attribution = "offmap_ambiguous"
            if role is None and guid not in catalog.offmap_units:
                found_roles = roles.get((identifier, guid), set())
                if "main" in found_roles:
                    role = "main"
                elif "transport" in found_roles:
                    # A transport keeps its own kill record and investment; only
                    # its classification follows the unit it carried, because the
                    # original game has no separate mechanised class.
                    role = "transport"
                    passengers = carried.get((identifier, guid), set())
                    if len(passengers) == 1:
                        transported = catalog.unit(next(iter(passengers)), payload["game_build"])
                        if transported.get("category") not in (None, "unknown"):
                            value = {**value, "category": transported["category"]}
                            attribution = "carried_category"
                    else:
                        # The vehicle keeps its own record but its carried
                        # classification cannot be determined for this match.
                        attribution = "transport_category_unresolved"
                else:
                    # Only trusted, complete production allows this claim: a
                    # unit missing from that list was produced by the AI (after
                    # a disconnect) or captured/scripted, and the official
                    # IsTransporter flag still decides its component role.
                    if ready.get(identifier, False):
                        role = "transport" if catalog.is_transport(guid) else "main"
                        attribution = "unpurchased_" + role
        return {"player_user_id": identifier, "unit_name": raw["unit_name"], "valuation": value,
                "role": role, "identity_status": raw["identity_status"], "attribution": attribution,
                "display_veterancy": raw.get("display_veterancy"), "is_ace": raw.get("is_ace")}

    def contribute(item, kind, value, *, uncertain_attribution=False):
        identifier = item["player_user_id"]
        if identifier is None:
            return
        attribution = str(item.get("attribution") or "")
        if attribution.startswith("unpurchased_"):
            # Units this player never produced (AI production after takeover, or
            # a captured/scripted unit) are counted, but recorded as evidence.
            unpurchased_events[identifier]["kill" if kind == "K" else "loss"] += 1
        for scope in SCOPES:
            if scope != "all_components" and item["role"] not in (scope, None):
                continue
            for metric in targets(identifier, item["valuation"], item["unit_name"], scope):
                if scope != "all_components" and item["role"] is None:
                    metric["unknown_role_count"] += 1
                    metric["unknown_" + ("kill" if kind == "K" else "loss") + "_scope_count"] += 1
                    continue
                if uncertain_attribution:
                    metric["unknown_" + ("kill" if kind == "K" else "loss") + "_attribution_count"] += 1
                    continue
                metric["kill_count" if kind == "K" else "loss_count"] += 1
                suffix = "kill" if kind == "K" else "loss"
                if item.get("attribution") == "offmap_observer":
                    metric["offmap_observer_" + suffix + "_count"] += 1
                elif item.get("attribution") == "carried_category":
                    metric["carried_category_" + suffix + "_count"] += 1
                elif item.get("attribution") == "transport_category_unresolved":
                    metric["category_unresolved_" + suffix + "_count"] += 1
                elif attribution.startswith("unpurchased_"):
                    metric["unpurchased_" + suffix + "_count"] += 1
                elif attribution == "offmap_ambiguous":
                    metric["offmap_ambiguous_" + suffix + "_count"] += 1
                if value["cost"] is None:
                    metric["unknown_" + ("kill" if kind == "K" else "loss") + "_value_count"] += 1
                else:
                    metric[kind + "_known_value"] += value["cost"]
                    metric["inferred_price_count"] += int(value["price_inferred"])

    events = []
    unpurchased_events = defaultdict(lambda: {"kill": 0, "loss": 0})
    identity_gaps = []
    unowned_kills = 0
    unowned_losses = 0
    friendly = 0
    for row in payload["rows"]:
        killer, victim = actor(row["killer"]), actor(row["victim"])
        k_id, v_id = killer["player_user_id"], victim["player_user_id"]
        if k_id is not None and v_id is not None:
            if roster[k_id]["alliance"] != roster[v_id]["alliance"]:
                contribute(killer, "K", victim["valuation"])
                if killer["valuation"]["guid"] is None:
                    identity_gaps.append((killer, "kill", {k_id}, False))
            else:
                friendly += 1
        elif k_id is not None:
            contribute(killer, "K", victim["valuation"], uncertain_attribution=True)
            if killer["valuation"]["guid"] is None:
                identity_gaps.append((killer, "kill", {k_id}, False))
        else:
            unowned_kills += 1
            possible_owners = {identifier for identifier, player in roster.items()
                               if v_id is None or player["alliance"] != roster[v_id]["alliance"]}
            identity_gaps.append((killer, "kill", possible_owners, True))
            for identifier, player in roster.items():
                if v_id is None or player["alliance"] != roster[v_id]["alliance"]:
                    for scope in SCOPES:
                        player_rows[(identifier, scope)]["unknown_kill_attribution_count"] += 1
        if v_id is not None:
            contribute(victim, "L", victim["valuation"])
            if victim["valuation"]["guid"] is None:
                identity_gaps.append((victim, "loss", {v_id}, False))
        else:
            unowned_losses += 1
            identity_gaps.append((victim, "loss", set(roster), True))
            for identifier in roster:
                for scope in SCOPES:
                    player_rows[(identifier, scope)]["unknown_loss_attribution_count"] += 1
        events.append({"row_index": row["row_index"], "game_time_seconds": row["game_time_seconds"],
                       "killer": killer, "victim": victim})
    for item, kind, owners, unknown_owner in identity_gaps:
        candidates = ({item["valuation"]["guid"]} if item["valuation"]["guid"] is not None else
                      {u["guid"] for u in catalog.by_name.get(item["unit_name"], ())})
        for (owner, _, _), metric in unit_rows.items():
            if owner in owners and metric["guid"] is not None and metric["guid"] in candidates:
                metric["unknown_" + kind + "_attribution_count"] += 1
        if unknown_owner and item["valuation"]["category"] != "unknown":
            for (owner, category, _), metric in category_rows.items():
                if owner in owners and category == item["valuation"]["category"]:
                    metric["unknown_" + kind + "_attribution_count"] += 1
    # Official unit counts (Stats +0x20/+0x24) vs the rows this report shows:
    # a positive difference is exactly the "aggregate counted it, the per-unit
    # timeline did not" case that explains 5-multiple point gaps.
    timeline_counts = defaultdict(lambda: {"kill": 0, "loss": 0})
    for event in events:
        killer_id, victim_id = event["killer"]["player_user_id"], event["victim"]["player_user_id"]
        if killer_id is not None and victim_id is not None and killer_id != victim_id:
            if roster[killer_id]["alliance"] != roster[victim_id]["alliance"]:
                timeline_counts[killer_id]["kill"] += 1
        if victim_id is not None:
            timeline_counts[victim_id]["loss"] += 1
    for (identifier, scope), metric in player_rows.items():
        metric["P_available"] = ready[identifier]
    for (identifier, key, scope), metric in unit_rows.items():
        metric["P_available"] = ready[identifier] and metric["guid"] is not None
    for (identifier, category, scope), metric in category_rows.items():
        metric["P_available"] = ready[identifier]
    players = []
    for identifier, raw in roster.items():
        control = (archived.get(identifier) or {}).get("control") or {"status": "unverified"}
        # A disconnected player's remaining units fight under AI control and are
        # produced by the AI without any player production request. When the
        # replay itself is gone (rotated archive) that evidence still decides.
        reasons = []
        unpurchased = dict(unpurchased_events.get(identifier, {"kill": 0, "loss": 0}))
        # Only this evidence decides: the game has no capture mechanic, so a
        # unit the player never produced that fights in the timeline was
        # produced by the AI after a takeover. A quiet stretch is not enough
        # (a player may just idle); it stays informational in `control`.
        if sum(unpurchased.values()) and ready.get(identifier, False):
            reasons.append("ai_production_detected")
        applicable = not reasons
        economy = _finish(player_rows[(identifier, "main")])
        official_kill, official_loss = raw["official_kill_points"], raw["official_loss_points"]
        gap = None
        if applicable:
            # The official table counts every recorded death, so compare it with
            # the all-components valuation rather than the main-only scope.
            total = _finish(player_rows[(identifier, "all_components")])
            gap = {"kill_points": (official_kill - total["K"] if isinstance(official_kill, int)
                                   and total["K"] is not None else None),
                   "loss_points": (official_loss - total["L"] if isinstance(official_loss, int)
                                   and total["L"] is not None else None),
                   "note": "官方总分与逐事件估值（全部组件）本应相等。差额为 5 的倍数时，通常是某个单位的损失/击杀未进入时间表（原版在无有效击杀者时只累加汇总、不写逐单位历史）；差额为个位时，说明官方逐事件按折算取整。绑定运输的整包价不计入死亡估值（实测整包被击毁仍按主单位价计）。"}
        row_count = timeline_counts.get(identifier, {"kill": 0, "loss": 0})
        official_kill_units = raw.get("official_kill_unit_count")
        official_loss_units = raw.get("official_loss_unit_count")
        unit_count_check = {
            "official_kill_units": official_kill_units, "official_loss_units": official_loss_units,
            "timeline_kill_rows": row_count["kill"], "timeline_loss_rows": row_count["loss"],
            "missing_kill_units": (official_kill_units - row_count["kill"]
                                   if isinstance(official_kill_units, int) else None),
            "missing_loss_units": (official_loss_units - row_count["loss"]
                                   if isinstance(official_loss_units, int) else None)}
        players.append({"player_user_id": identifier, "name": raw["name"], "alliance": raw["alliance"],
            "official_kill_points": official_kill, "official_loss_points": official_loss,
            "control": control, "applicable": applicable,
            "not_applicable_reason": ";".join(reasons) if reasons else None,
            "unpurchased_events": unpurchased,
            "unit_count_check": unit_count_check,
            "derived_gap": gap,
            "economy": economy,
            "economy_by_scope": {scope: _finish(player_rows[(identifier, scope)]) for scope in SCOPES},
            "economy_scopes": [_finish(player_rows[(identifier, scope)]) for scope in SCOPES],
            "units": [_finish(r) for (owner, _, _), r in sorted(unit_rows.items()) if owner == identifier],
            "categories": [_finish(r) for (owner, _, _), r in sorted(category_rows.items()) if owner == identifier],
            "production_status": "requests_estimate" if ready[identifier] else "unavailable",
            "cancellation_adjustment": summaries[identifier].get("cancellation_adjustment", {})})
    return {"players": players, "events": events, "production_inputs": production,
            "coverage": {"event_rows": len(events), "unattributed_kill_events": unowned_kills,
                         "unattributed_loss_events": unowned_losses, "friendly_events": friendly},
            "catalog": catalog.provenance(), "model_version": MODEL_VERSION}


class CombatReader:
    """Read-only views; construction never creates, migrates or repairs a DB."""
    def __init__(self, path):
        self.path = Path(path).resolve()

    def status(self):
        try:
            with _read(self.path) as db:
                row = db.execute("SELECT COUNT(*) AS n,COALESCE(SUM(conflict),0) AS conflicts FROM match_report_selection").fetchone()
                return {"available": True, "match_count": row["n"], "conflict_count": row["conflicts"], "model_version": MODEL_VERSION}
        except (OSError, sqlite3.Error):
            return {"available": False, "match_count": 0, "conflict_count": 0, "model_version": MODEL_VERSION}

    def match(self, server_sha256):
        if not isinstance(server_sha256, str) or not HASH.fullmatch(server_sha256):
            raise ValueError("invalid_match_sha256")
        base = {"schema": "sd2-combat-match-v1", "available": False, "server_sha256": server_sha256, "players": []}
        try:
            with _read(self.path) as db:
                row = db.execute("SELECT * FROM match_report_selection WHERE server_sha256=?", (server_sha256,)).fetchone()
                if row is None:
                    return {**base, "reason": "no_selected_report"}
                result = json.loads(row["payload"])
                return {**base, **result, "available": True, "round_key": row["round_key"],
                        "selection": {"submission_id": row["submission_id"], "conflict": bool(row["conflict"]),
                                      "content_sha256": row["content_sha256"], "input_sha256": row["input_sha256"]}}
        except (OSError, sqlite3.Error):
            return {**base, "reason": "combat_database_unavailable"}

    def player(self, identifier, limit=20):
        if not isinstance(identifier, str) or not PLAYER.fullmatch(identifier) or int(identifier) >= 2**64 - 1:
            raise ValueError("invalid_player_user_id")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_match_window")
        base = {"schema": "sd2-combat-player-v1", "available": False, "player_user_id": identifier,
                "window_limit": limit, "match_count": 0, "official_totals": None, "economy": None,
                "units": [], "categories": [], "matches": [], "has_conflicts": False}
        try:
            with _read(self.path) as db:
                window = db.execute("SELECT round_key FROM archive_player_rounds WHERE player_user_id=? ORDER BY observed_at DESC,round_key DESC LIMIT ?",
                                    (identifier, limit)).fetchall()
                keys = [r["round_key"] for r in window]
                if not keys:
                    return {**base, "reason": "no_archived_player_matches"}
                placeholders = ",".join("?" for _ in keys)
                rows = db.execute("SELECT p.payload,s.server_sha256,s.submission_id,s.conflict,s.round_key FROM player_match_combat p "
                                  "JOIN match_report_selection s ON s.round_key=p.round_key WHERE p.player_user_id=? AND p.round_key IN ("
                                  + placeholders + ") ORDER BY s.observed_at DESC,s.round_key DESC", [identifier, *keys]).fetchall()
                if not rows:
                    return {**base, "reason": "no_selected_report", "archived_window_count": len(keys)}
                matches = []
                applicable = []
                units, categories = defaultdict(list), defaultdict(list)
                for row in rows:
                    value = json.loads(row["payload"])
                    matches.append({**value, "server_sha256": row["server_sha256"], "round_key": row["round_key"],
                                    "submission_id": row["submission_id"], "conflict": bool(row["conflict"])})
                    if value.get("applicable", True) is False:
                        # A disconnected player's units fought under AI control,
                        # so this match must not feed the P/K aggregate.
                        continue
                    applicable.append(value)
                    for unit in value["units"]:
                        units[(unit["unit_key"], unit["role_scope"])].append(unit)
                    for cat in value["categories"]:
                        categories[(cat["category"], cat["role_scope"])].append(cat)
                unit_rows = [{**{k: rows[0][k] for k in ("unit_key", "guid", "name", "category", "catalog_price_status")},
                              "catalog_unit_cost": rows[0]["catalog_unit_cost"] if len({r["catalog_unit_cost"] for r in rows}) == 1 else None,
                              **_summarize(rows)} for _, rows in sorted(units.items())]
                cat_rows = [{"category": key[0], **_summarize(values)} for key, values in sorted(categories.items())]
                return {**base, "available": True, "name": matches[0]["name"], "match_count": len(matches),
                        "applicable_match_count": len(applicable),
                        "not_applicable_match_count": len(matches) - len(applicable),
                        "archived_window_count": len(keys), "has_conflicts": any(m["conflict"] for m in matches),
                        "official_totals": {
                            "kill_points": sum(m["official_kill_points"] for m in applicable) if applicable else None,
                            "loss_points": sum(m["official_loss_points"] for m in applicable) if applicable else None,
                            "sample_count": len(applicable),
                            "not_applicable_match_count": len(matches) - len(applicable)},
                        "economy": _summarize([m["economy"] for m in applicable]),
                        "economy_by_scope": {scope: _summarize([m["economy_by_scope"][scope] for m in applicable])
                                             for scope in SCOPES},
                        "units": unit_rows,
                        "categories": cat_rows, "matches": matches, "model_version": MODEL_VERSION}
        except (OSError, sqlite3.Error):
            return {**base, "reason": "combat_database_unavailable"}


class CombatStore(CombatReader):
    def __init__(self, path, *, catalog=None):
        super().__init__(path)
        self.catalog = catalog or Catalog()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name IN ('reports','submissions') LIMIT 1").fetchone():
                raise ValueError("combat_database_must_be_separate")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS source_reports(submission_id TEXT PRIMARY KEY,round_key TEXT NOT NULL,
                    server_sha256 TEXT NOT NULL,payload_sha256 TEXT NOT NULL,content_sha256 TEXT NOT NULL,received_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS match_report_selection(round_key TEXT PRIMARY KEY,server_sha256 TEXT NOT NULL,
                    submission_id TEXT NOT NULL,content_sha256 TEXT NOT NULL,input_sha256 TEXT NOT NULL,
                    conflict INTEGER NOT NULL,observed_at TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS selected_sha ON match_report_selection(server_sha256);
                CREATE TABLE IF NOT EXISTS normalized_events(round_key TEXT NOT NULL,row_index INTEGER NOT NULL,
                    payload TEXT NOT NULL,PRIMARY KEY(round_key,row_index));
                CREATE TABLE IF NOT EXISTS production_inputs(round_key TEXT NOT NULL,player_user_id TEXT NOT NULL,
                    unit_index INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(round_key,player_user_id,unit_index));
                CREATE TABLE IF NOT EXISTS player_match_combat(round_key TEXT NOT NULL,player_user_id TEXT NOT NULL,
                    payload TEXT NOT NULL,PRIMARY KEY(round_key,player_user_id));
                CREATE TABLE IF NOT EXISTS player_unit_economy(round_key TEXT NOT NULL,player_user_id TEXT NOT NULL,
                    unit_key TEXT NOT NULL,role_scope TEXT NOT NULL,payload TEXT NOT NULL,
                    PRIMARY KEY(round_key,player_user_id,unit_key,role_scope));
                CREATE TABLE IF NOT EXISTS player_category_economy(round_key TEXT NOT NULL,player_user_id TEXT NOT NULL,
                    category TEXT NOT NULL,role_scope TEXT NOT NULL,payload TEXT NOT NULL,
                    PRIMARY KEY(round_key,player_user_id,category,role_scope));
                CREATE TABLE IF NOT EXISTS archive_player_rounds(round_key TEXT NOT NULL,player_user_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,server_sha256 TEXT NOT NULL,PRIMARY KEY(round_key,player_user_id));
                CREATE INDEX IF NOT EXISTS archive_player_recent ON archive_player_rounds(player_user_id,observed_at DESC);
                CREATE TABLE IF NOT EXISTS derivation_runs(input_sha256 TEXT PRIMARY KEY,round_key TEXT NOT NULL,
                    model_version TEXT NOT NULL,catalog TEXT NOT NULL,derived_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS derivation_inputs(key TEXT PRIMARY KEY,value TEXT NOT NULL,
                    updated_at REAL NOT NULL);
            """)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.execute("PRAGMA synchronous=FULL")
                yield db
        finally:
            db.close()

    @staticmethod
    def _archive(sha, paths):
        records = []
        for path in paths:
            with _read(path) as db:
                record = db.execute("SELECT match_key,payload,analyzed_at,completion_status FROM reports WHERE sha256=?", (sha,)).fetchone()
                if record is not None:
                    document = json.loads(record["payload"])
                    if (record["completion_status"] != "finished" or document.get("source", {}).get("sha256") != sha
                            or document.get("match", {}).get("completion", {}).get("status") != "finished"):
                        raise ValueError("invalid_finished_archive")
                    records.append((record, document))
        if not records:
            raise ValueError("accepted_archive_unavailable")
        # The configured primary archive supplies the established match key.
        # Conflicting denominators are not resolved by selecting a larger one.
        fingerprints = {_digest({"match": r[1]["match"], "players": r[1].get("players", [])}) for r in records}
        if len(fingerprints) != 1:
            raise ValueError("archive_analysis_conflict")
        return records[0]

    def _index_archives(self, paths):
        indexed = {}
        sha_keys = {}
        for path in paths:
            with _read(path) as db:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='report_players' AND type='table'").fetchone()
                if not exists:
                    continue
                rows = db.execute("SELECT r.sha256,r.match_key,r.analyzed_at,rp.player_id,rp.observed_at FROM reports r "
                    "JOIN report_players rp ON rp.sha256=r.sha256 WHERE r.completion_status='finished' AND rp.participated=1")
                for row in rows:
                    key = sha_keys.setdefault(row["sha256"], row["match_key"])
                    indexed[(key, row["player_id"])] = (row["observed_at"] or row["analyzed_at"], row["sha256"])
        with self._connection() as db:
            for (key, identifier), (observed, sha) in indexed.items():
                db.execute("INSERT OR REPLACE INTO archive_player_rounds VALUES(?,?,?,?)", (key, identifier, observed, sha))

    def _stored_inputs(self):
        with self._connection() as db:
            return {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM derivation_inputs")}

    def _store_inputs(self, values):
        if not values:
            return
        now = time.time()
        with self._connection() as db:
            db.executemany("INSERT OR REPLACE INTO derivation_inputs VALUES(?,?,?)",
                           [(key, value, now) for key, value in values.items()])

    def inputs_signature(self, inbox_path, archive_paths, *, catalog=None):
        """Digest everything a cycle reads; ``None`` when it cannot be certified."""
        inbox = _inbox_signature(Path(inbox_path).resolve())
        archives = archive_signature(tuple(Path(path).resolve() for path in archive_paths))
        if inbox is None or archives is None:
            return None
        return _digest({"inbox": inbox, "archives": archives,
                        "catalog": (catalog or self.catalog).provenance(), "version": MODEL_VERSION})

    @staticmethod
    def _archive_stamp(sha, paths):
        """Cheap identity of the archive report a round was derived from."""
        stamps = []
        for path in paths:
            with _read(path) as db:
                row = db.execute("SELECT match_key,analyzed_at,completion_status FROM reports WHERE sha256=?",
                                 (sha,)).fetchone()
            stamps.append(None if row is None else [row["match_key"], row["analyzed_at"], row["completion_status"]])
        return stamps

    def refresh(self, inbox_path, archive_paths, *, catalog=None, limit=100, signature=None):
        from .receiver import SUPPORTED_SCHEMAS, validate_payload
        inbox_path = Path(inbox_path).resolve()
        paths = tuple(dict.fromkeys(Path(p).resolve() for p in archive_paths))
        if self.path == inbox_path or self.path in paths or inbox_path in paths:
            raise ValueError("combat_database_must_be_separate")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid_derivation_limit")
        current_catalog = catalog or self.catalog
        self.catalog = current_catalog
        stored = self._stored_inputs()
        if signature is None:
            signature = self.inputs_signature(inbox_path, paths, catalog=current_catalog)
        if signature is not None and stored.get("inputs") == signature:
            # Nothing an accepted submission, archive or catalog could change
            # since the last completed cycle, so re-reading every payload would
            # only burn CPU on a host that also runs the game server.
            return {"new_sources": 0, "derived_matches": 0, **self.status()}
        archives = archive_signature(paths)
        if archives is None or stored.get("archives") != archives:
            self._index_archives(paths)
        with _read(inbox_path) as inbox, self._connection() as db:
            accepted = inbox.execute("SELECT submission_id,server_sha256,payload_sha256,received_at FROM submissions "
                                     "WHERE status='accepted' ORDER BY received_at,submission_id").fetchall()
            existing = {r[0] for r in db.execute("SELECT submission_id FROM source_reports")}
            processed = 0
            for meta in accepted:
                if meta["submission_id"] in existing or processed >= limit:
                    continue
                raw = inbox.execute("SELECT payload FROM submissions WHERE submission_id=?", (meta["submission_id"],)).fetchone()
                payload = json.loads(raw[0])
                validate_payload(payload)
                if payload["schema"] not in SUPPORTED_SCHEMAS:
                    continue
                record, archive = self._archive(meta["server_sha256"], paths)
                key = record["match_key"]
                db.execute("INSERT INTO source_reports VALUES(?,?,?,?,?,?)", (meta["submission_id"], key,
                    meta["server_sha256"], meta["payload_sha256"], _content_hash(payload), meta["received_at"]))
                processed += 1
            rounds = db.execute("SELECT DISTINCT round_key FROM source_reports ORDER BY round_key").fetchall()
            stamps = {key[len("round:"):]: value for key, value in stored.items() if key.startswith("round:")}
            derived = 0
            for entry in rounds:
                key = entry["round_key"]
                sources = db.execute("SELECT * FROM source_reports WHERE round_key=? ORDER BY received_at,submission_id", (key,)).fetchall()
                previous = db.execute("SELECT * FROM match_report_selection WHERE round_key=?", (key,)).fetchone()
                chosen = next((r for r in sources if previous is not None and r["submission_id"] == previous["submission_id"]), sources[0])
                meta = inbox.execute("SELECT status,server_sha256,payload_sha256 FROM submissions WHERE submission_id=?",
                                     (chosen["submission_id"],)).fetchone()
                if meta is None or meta["status"] != "accepted" or meta["server_sha256"] != chosen["server_sha256"]:
                    continue
                conflict = len({r["content_sha256"] for r in sources}) > 1
                stamps[key] = _digest({"submission": meta["payload_sha256"],
                    "archive": self._archive_stamp(chosen["server_sha256"], paths),
                    "catalog": current_catalog.provenance(), "version": MODEL_VERSION, "conflict": conflict})
                if stored.get("round:" + key) == stamps[key]:
                    # Same submission, same archive revision, same catalog: the
                    # stored derivation is still the answer, so skip both the
                    # payload parse and the archive report parse.
                    continue
                raw = inbox.execute("SELECT payload FROM submissions WHERE submission_id=?",
                                    (chosen["submission_id"],)).fetchone()
                payload = json.loads(raw["payload"])
                validate_payload(payload)
                record, archive = self._archive(chosen["server_sha256"], paths)
                fingerprint = _digest({"submission": chosen["payload_sha256"], "archive": archive,
                    "catalog": current_catalog.provenance(), "version": MODEL_VERSION, "conflict": conflict})
                if previous is not None and previous["input_sha256"] == fingerprint:
                    continue
                result = derive_match(payload, archive, current_catalog)
                observed = record["analyzed_at"]
                result.update(schema="sd2-combat-match-v1", server_sha256=chosen["server_sha256"],
                              report_submission_id=chosen["submission_id"])
                db.execute("INSERT OR REPLACE INTO match_report_selection VALUES(?,?,?,?,?,?,?,?)", (key,
                    chosen["server_sha256"], chosen["submission_id"], chosen["content_sha256"], fingerprint, int(conflict), observed, _json(result)))
                for table in ("normalized_events", "production_inputs", "player_match_combat", "player_unit_economy", "player_category_economy"):
                    db.execute("DELETE FROM " + table + " WHERE round_key=?", (key,))
                for event in result["events"]:
                    db.execute("INSERT INTO normalized_events VALUES(?,?,?)", (key, event["row_index"], _json(event)))
                for index, value in enumerate(result["production_inputs"]):
                    db.execute("INSERT INTO production_inputs VALUES(?,?,?,?)", (key, value["player_user_id"], index, _json(value)))
                for value in result["players"]:
                    identifier = value["player_user_id"]
                    db.execute("INSERT INTO player_match_combat VALUES(?,?,?)", (key, identifier, _json(value)))
                    db.execute("INSERT OR IGNORE INTO archive_player_rounds VALUES(?,?,?,?)", (key, identifier, observed, chosen["server_sha256"]))
                    for unit in value["units"]:
                        db.execute("INSERT INTO player_unit_economy VALUES(?,?,?,?,?)", (key, identifier, unit["unit_key"], unit["role_scope"], _json(unit)))
                    for cat in value["categories"]:
                        db.execute("INSERT INTO player_category_economy VALUES(?,?,?,?,?)", (key, identifier, cat["category"], cat["role_scope"], _json(cat)))
                db.execute("INSERT OR IGNORE INTO derivation_runs VALUES(?,?,?,?,?)", (fingerprint, key, MODEL_VERSION,
                    _json(current_catalog.provenance()), time.time()))
                derived += 1
        settled = {"round:" + key: value for key, value in stamps.items()}
        if archives is not None:
            settled["archives"] = archives
        final = self.inputs_signature(inbox_path, paths, catalog=current_catalog)
        if final is not None:
            settled["inputs"] = final
        self._store_inputs(settled)
        return {"new_sources": processed, "derived_matches": derived, **self.status()}
