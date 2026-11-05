"""Versioned catalog valuation; native identities and name inferences stay distinct."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import re


GUID = re.compile(r"[0-9a-f]{32}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
DEFAULT_CATALOG = Path(__file__).resolve().parents[1] / "data/unit-guid-map.json"
CATEGORIES = frozenset(("recon", "infantry", "tank", "support", "anti_tank", "anti_air",
                        "artillery", "air", "defense"))


def finite_price(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) and 0 <= value <= 2**31 - 1 else None
    except OverflowError:
        return None


class Catalog:
    def __init__(self, path=DEFAULT_CATALOG):
        self.path = Path(path).resolve()
        raw = self.path.read_bytes()
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("catalog_too_large")
        document = json.loads(raw)
        if isinstance(document, dict):
            build = document.get("game_build")
            if isinstance(build, str) and re.fullmatch(r"[1-9][0-9]{0,9}", build):
                document["game_build"] = int(build)
        if (not isinstance(document, dict) or document.get("schema") != 1
                or type(document.get("game_build")) is not int
                or not 0 < document["game_build"] < 2**31
                or not isinstance(document.get("units"), dict)
                or not HASH.fullmatch(document.get("catalog_sha256", ""))):
            raise ValueError("invalid_price_catalog")
        hashes = document.get("source_hashes", {})
        if (not isinstance(hashes, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                              or not HASH.fullmatch(v) for k, v in hashes.items())):
            raise ValueError("invalid_catalog_source_hashes")
        self.game_build = document["game_build"]
        self.catalog_sha256 = document["catalog_sha256"]
        self.guid_map_sha256 = hashlib.sha256(raw).hexdigest()
        self.source_hashes = hashes
        self.units = {}
        self.by_name = defaultdict(list)
        for guid, unit in document["units"].items():
            if (not isinstance(guid, str) or not GUID.fullmatch(guid) or not isinstance(unit, dict)
                    or not isinstance(unit.get("name"), str) or not unit["name"]
                    or not isinstance(unit.get("descriptor"), str)):
                raise ValueError("invalid_price_catalog_unit")
            copied = {**unit, "guid": guid}
            self.units[guid] = copied
            self.by_name[unit["name"]].append(copied)
        self.offmap_units = {}
        self.offmap_observers = {}
        self.offmap_sha256 = None
        self.transport_guids = frozenset()
        self.transport_sha256 = None
        self._load_offmap()
        self._load_transport_definitions()

    def _load_transport_definitions(self):
        """Official IsTransporter flags, used when a unit has no purchase."""
        sibling = self.path.with_name("transport-classification.json")
        try:
            raw = sibling.read_bytes()
        except (OSError, ValueError):
            return
        if len(raw) > 32 * 1024 * 1024:
            return
        try:
            document = json.loads(raw)
        except ValueError:
            return
        if (not isinstance(document, dict) or document.get("schema") != 1
                or document.get("unit_guid_map_sha256") != self.guid_map_sha256
                or not isinstance(document.get("units"), dict)):
            return
        self.transport_guids = frozenset(guid for guid, unit in document["units"].items()
                                         if isinstance(guid, str) and GUID.fullmatch(guid)
                                         and isinstance(unit, dict) and unit.get("is_transporter") is True)
        self.transport_sha256 = hashlib.sha256(raw).hexdigest()

    def is_transport(self, guid):
        return isinstance(guid, str) and guid in self.transport_guids

    def _load_offmap(self):
        """Optional off-map strike links; never guessed from names or calibres."""
        sibling = self.path.with_name("offmap-map.json")
        try:
            raw = sibling.read_bytes()
        except (OSError, ValueError):
            return
        if len(raw) > 16 * 1024 * 1024:
            return
        try:
            document = json.loads(raw)
        except ValueError:
            return
        if (not isinstance(document, dict) or document.get("schema") != 1
                or type(document.get("game_build")) is not int
                or document["game_build"] != self.game_build
                or document.get("unit_guid_map_sha256") != self.guid_map_sha256
                or not isinstance(document.get("links"), dict)
                or not isinstance(document.get("offmap_units"), dict)):
            return
        links = {}
        for offmap, observers in document["links"].items():
            if (not isinstance(offmap, str) or not GUID.fullmatch(offmap)
                    or not isinstance(observers, list) or not observers
                    or any(not isinstance(value, str) or not GUID.fullmatch(value) for value in observers)):
                return
            links[offmap] = tuple(observers)
        offmap_units = {key: value for key, value in document["offmap_units"].items()
                        if isinstance(key, str) and GUID.fullmatch(key) and isinstance(value, dict)}
        self.offmap_units = offmap_units
        self.offmap_observers = links
        self.offmap_sha256 = hashlib.sha256(raw).hexdigest()

    def provenance(self):
        return {"game_build": self.game_build, "guid_map_sha256": self.guid_map_sha256,
                "catalog_sha256": self.catalog_sha256, "source_hashes": dict(self.source_hashes),
                "offmap": {"sha256": self.offmap_sha256, "linked_offmap_units": len(self.offmap_observers)},
                "transports": {"sha256": self.transport_sha256, "definition_count": len(self.transport_guids)}}

    def observer_for(self, guid, purchased_main):
        """Return the observer GUID only when exactly one purchase can own it."""
        observers = self.offmap_observers.get(guid, ())
        if not observers:
            return None
        owned = {observer for observer in observers if observer in purchased_main}
        return next(iter(owned)) if len(owned) == 1 else None

    @staticmethod
    def _empty(status, *, guid=None):
        return {"guid": guid, "catalog_id": None, "descriptor": None, "name": None,
                "category": "unknown", "cost": None, "price_status": status,
                "price_inferred": False, "candidate_count": 0}

    def unit(self, guid, game_build):
        if type(game_build) is not int or game_build != self.game_build:
            return self._empty("catalog_build_mismatch", guid=guid)
        if not isinstance(guid, str) or not GUID.fullmatch(guid):
            return self._empty("missing_guid")
        unit = self.units.get(guid)
        if unit is None:
            return self._empty("unknown_guid", guid=guid)
        cost = finite_price(unit.get("cost"))
        return {"guid": guid, "catalog_id": unit.get("catalog_id"), "descriptor": unit["descriptor"],
                "name": unit["name"], "category": unit.get("category") if unit.get("category") in CATEGORIES else "unknown",
                "cost": cost, "price_status": "catalog_guid" if cost is not None else "missing_catalog_price",
                "price_inferred": False, "candidate_count": 1}

    def actor(self, actor, game_build):
        """Never use a guessed GUID or choose one of several candidate prices."""
        if actor.get("identity_status") == "resolved_native_stats":
            result = self.unit(actor.get("unit_guid"), game_build)
            if result["cost"] is not None:
                result["price_status"] = "resolved_native_stats"
            return result
        if type(game_build) is not int or game_build != self.game_build:
            return self._empty("catalog_build_mismatch")
        candidates = self.by_name.get(actor.get("unit_name"), ())
        if not candidates:
            return self._empty("unmatched_name")
        # Exact display-name equality can establish a common catalog value,
        # but never establishes a native identity. Do not prune by a deck.
        prices = [finite_price(unit.get("cost")) for unit in candidates]
        categories = {unit.get("category") for unit in candidates}
        result = self._empty("ambiguous_name_price")
        result.update(name=actor.get("unit_name"), candidate_count=len(candidates))
        if len(categories) == 1 and next(iter(categories)) in CATEGORIES:
            result["category"] = next(iter(categories))
        if any(value is None for value in prices):
            result["price_status"] = "missing_catalog_price"
        elif len(set(prices)) == 1:
            result.update(cost=prices[0], price_status="inferred_common_name_price", price_inferred=True)
        return result
