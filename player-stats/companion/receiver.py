"""Standalone client-report inbox; never writes the production replay store."""

from __future__ import annotations

from collections import Counter
from contextlib import closing, contextmanager
import datetime
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import uuid

from . import open_readonly_database
from .upload import MAX_PAYLOAD_BYTES
from .server_sessions import ServerSessionIndex
from .enrollment import (EnrollmentError, INSTALLATIONS_PATH, MAX_REGISTRATION_BYTES,
                         client_address)


API_PATH = "/api/companion/v3/task-reports"
RECEIPTS_PATH = "/api/companion/v3/receipts"
COMBAT_PATH = "/api/companion/v3/combat"
CATALOG_PATH = "/api/companion/v3/catalog"
ROOMS_PATH = "/api/companion/v3/rooms"
CATALOG_MAX_BYTES = 64 * 1024
CATALOG_DEFAULT_INTERVAL = 600.0
MAX_ROWS = 20000
TASK_SCHEMA = "sd2-task-report-v4"
# Stored v3 submissions stay readable; new clients only send v4.
LEGACY_SCHEMA = "sd2-task-report-v3"
SUPPORTED_SCHEMAS = (TASK_SCHEMA, LEGACY_SCHEMA)
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ROOM = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_PRIVATE_FIELD = re.compile(r"(?:^|_)(?:pid|ptr|pointer|address|addresses|process_id|process_key|memory)(?:$|_)", re.I)


class ReceiverError(ValueError):
    def __init__(self, code, status_code=400):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _text(value, maximum, *, nonempty=False):
    if (not isinstance(value, str) or len(value) > maximum or (nonempty and not value)
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ReceiverError("invalid_text")
    return value


def _integer(value, maximum, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ReceiverError("invalid_integer")
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReceiverError("duplicate_json_property")
        result[key] = value
    return result


def _nonfinite(_):
    raise ReceiverError("nonfinite_json_number")


def _player_user_id(value):
    # The native uint64 invalid sentinel is represented as null by the reader.
    # Require canonical decimal strings; never infer an ID from a name.
    if (not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,19})", value)
            or int(value) >= 2**64 - 1):
        raise ReceiverError("invalid_native_player_user_id")
    return value


def _native_players(players, schema=TASK_SCHEMA):
    if not isinstance(players, list) or len(players) > 20:
        raise ReceiverError("invalid_native_players")
    required = {"row_index", "name", "player_user_id", "alliance", "alliance_display_value",
                "official_kill_points", "official_loss_points"}
    # v4 adds the official per-player unit counts (Stats +0x20/+0x24); they may
    # be null when the native statistics were unavailable for that match.
    optional = {"official_kill_unit_count", "official_loss_unit_count"}
    for index, player in enumerate(players):
        if not isinstance(player, dict):
            raise ReceiverError("invalid_native_player_fields")
        if schema == TASK_SCHEMA:
            if set(player) - required - optional or required - set(player):
                raise ReceiverError("invalid_native_player_fields")
        elif set(player) != required:
            raise ReceiverError("invalid_native_player_fields")
        if _integer(player["row_index"], 19) != index:
            raise ReceiverError("invalid_native_player_index")
        _text(player["name"], 256)
        _integer(player["official_kill_points"], 2**32 - 1)
        _integer(player["official_loss_points"], 2**32 - 1)
        for key in optional:
            if key in player and player[key] is not None:
                _integer(player[key], 2**32 - 1)
        if player["player_user_id"] is not None:
            _player_user_id(player["player_user_id"])
        display = _integer(player["alliance_display_value"], 4)
        if display not in (0, 1, 4):
            raise ReceiverError("invalid_native_player_alliance")
        alliance = player["alliance"]
        if display == 4:
            if alliance is not None:
                raise ReceiverError("invalid_native_player_alliance")
        elif _integer(alliance, 1) != display:
            raise ReceiverError("invalid_native_player_alliance")


def _native_roster(players):
    """Return a complete ID/alliance multiset, or no usable association."""
    _native_players(players)
    if not players or any(player["player_user_id"] is None or player["alliance"] is None for player in players):
        return None
    return Counter((player["player_user_id"], player["alliance"]) for player in players)


def _archive_roster(players):
    if not isinstance(players, list) or not 1 <= len(players) <= 64:
        return None
    result = Counter()
    for player in players:
        if not isinstance(player, dict):
            return None
        if "is_observer" in player and not isinstance(player["is_observer"], bool):
            return None
        if player.get("is_observer") is True:
            continue
        identifier = _player_user_id(player.get("id"))
        alliance = _integer(player.get("alliance"), 1)
        result[(identifier, alliance)] += 1
    return result or None


def decode_payload(data):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_PAYLOAD_BYTES:
        raise ReceiverError("invalid_payload_size", 413)
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_nonfinite)
    except ReceiverError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise ReceiverError("invalid_json") from None


def _exact_fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ReceiverError("invalid_submission_fields")


def _v3_actor(actor, players):
    fields = {"player_name", "unit_name", "soldier_name", "unit_label_texture_token", "xp_texture_token",
              "display_veterancy", "player_label_color_token", "is_ace", "unit_guid", "player_user_id", "identity_status"}
    _exact_fields(actor, fields)
    for key in ("player_name", "unit_name", "soldier_name"):
        _text(actor[key], 512)
    for key in ("unit_label_texture_token", "xp_texture_token", "display_veterancy", "player_label_color_token"):
        _integer(actor[key], 2**32 - 1)
    if type(actor["is_ace"]) is not bool:
        raise ReceiverError("invalid_actor_is_ace")
    identity = actor["identity_status"]
    if identity not in ("resolved_native_stats", "unresolved", "ambiguous"):
        raise ReceiverError("invalid_actor_identity_status")
    guid = actor["unit_guid"]
    if identity == "resolved_native_stats":
        if not isinstance(guid, str) or not re.fullmatch(r"[0-9a-f]{32}", guid):
            raise ReceiverError("resolved_actor_guid_required")
    elif guid is not None:
        raise ReceiverError("unresolved_actor_must_not_claim_guid")
    if actor["player_user_id"] is not None:
        _player_user_id(actor["player_user_id"])
        matches = [p for p in players if p["name"] == actor["player_name"]]
        if len(matches) != 1 or matches[0]["player_user_id"] != actor["player_user_id"]:
            raise ReceiverError("actor_player_identity_mismatch")


def validate_payload(payload):
    """Accept v4 (and stored v3), preserving every original event and its order."""
    if not isinstance(payload, dict):
        raise ReceiverError("invalid_submission_fields")
    if payload.get("schema") not in SUPPORTED_SCHEMAS:
        raise ReceiverError("unsupported_schema")
    required = {"schema", "submission_id", "installation_id", "session_id", "game_build", "source",
                "binding_mode", "native_players", "rows", "report_data", "client_context_evidence",
                "exe_sha256", "report_generation"}
    optional = {"map", "server_name", "locale", "extractor_version", "report_scope",
                "client_replay_sha256", "room_hint", "server_epoch"}
    _exact_fields(payload, required, optional)
    if payload["source"] != "player_client_official_task_report":
        raise ReceiverError("invalid_client_source")
    if not isinstance(payload["submission_id"], str) or not _HASH.fullmatch(payload["submission_id"]):
        raise ReceiverError("invalid_submission_id")
    try:
        installation = payload["installation_id"]
        if not isinstance(installation, str) or str(uuid.UUID(installation)) != installation.lower():
            raise ValueError
    except (ValueError, AttributeError):
        raise ReceiverError("invalid_installation_id") from None
    _text(payload["session_id"], 128, nonempty=True)
    _integer(payload["game_build"], 2**31 - 1, minimum=1)
    if payload["binding_mode"] not in ("replay_sha256", "native_session"):
        raise ReceiverError("invalid_binding_mode")
    replay = payload["binding_mode"] == "replay_sha256"
    for key, maximum in (("map", 512), ("server_name", 256), ("locale", 64), ("extractor_version", 64)):
        if key in payload:
            _text(payload[key], maximum, nonempty=key == "map")
    if replay and not payload.get("map"):
        raise ReceiverError("replay_map_required")
    for key in ("client_replay_sha256", "exe_sha256"):
        if key in payload and (not isinstance(payload[key], str) or not _HASH.fullmatch(payload[key])):
            raise ReceiverError("invalid_client_fingerprint")
    if replay:
        if "client_replay_sha256" not in payload:
            raise ReceiverError("full_replay_sha256_required")
    elif any(key in payload for key in ("client_header_sha256", "client_replay_sha256")):
        raise ReceiverError("native_live_must_not_use_client_replay_hash")
    if "room_hint" in payload and (not isinstance(payload["room_hint"], str) or not _ROOM.fullmatch(payload["room_hint"])):
        raise ReceiverError("invalid_room_hint")
    if "server_epoch" in payload:
        _text(payload["server_epoch"], 128, nonempty=True)
    if "report_scope" in payload and payload["report_scope"] != "native_published_participant_timeline":
        raise ReceiverError("unsupported_report_scope")
    _integer(payload["report_generation"], 2**64 - 1, minimum=1)
    _native_players(payload["native_players"])
    identifiers = [p["player_user_id"] for p in payload["native_players"] if p["player_user_id"] is not None]
    if len(set(identifiers)) != len(identifiers):
        raise ReceiverError("duplicate_native_player_id")
    evidence = payload["client_context_evidence"]
    if not isinstance(evidence, dict):
        raise ReceiverError("invalid_context_evidence")
    _display_fields(evidence)
    if replay:
        observed = (evidence.get("association") in ("observed_game_file_read", "observed_game_file_write")
                    and evidence.get("file_body_io_observed") is True)
        selected = (evidence.get("association") == "user_selected_local_replay"
                    and evidence.get("full_replay_sha256_verified") is True
                    and evidence.get("roster_check") == "exact_player_user_id_alliance_multiset"
                    and "file_body_io_observed" not in evidence)
        # Explicitly selected files do not pretend to be observed game I/O.
        # Both paths still require the exact archived bytes, session, build,
        # map and full ID/alliance roster in ArchiveIndex._replay.
        if not (observed or selected):
            raise ReceiverError("observed_replay_file_access_required")
    else:
        native_kind = evidence.get("native_source_kind")
        association = evidence.get("association")
        live_context = native_kind == "network_live" and association == "native_game_session"
        replay_context = native_kind == "replay" and association == "native_replay_session"
        if (not (live_context or replay_context)
                or evidence.get("session_origin") != "native_game_setup"
                or any(evidence.get(k) is not True for k in
                       ("native_game_ended", "observed_running", "report_observed_after_end"))
                or not isinstance(evidence.get("context_epoch"), str)
                or not _HASH.fullmatch(evidence["context_epoch"])
                or replay_context and not payload.get("map")):
            raise ReceiverError("native_live_session_evidence_required")
    rows = payload["rows"]
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ReceiverError("invalid_rows")
    for index, row in enumerate(rows):
        _exact_fields(row, {"row_index", "game_time_seconds", "time_display", "killer", "victim"})
        if _integer(row["row_index"], MAX_ROWS - 1) != index:
            raise ReceiverError("invalid_row_index")
        seconds = row["game_time_seconds"]
        if type(seconds) not in (int, float):
            raise ReceiverError("invalid_row_time")
        try:
            if not math.isfinite(seconds) or seconds < 0:
                raise ReceiverError("invalid_row_time")
        except OverflowError:
            raise ReceiverError("invalid_row_time") from None
        _text(row["time_display"], 64)
        _v3_actor(row["killer"], payload["native_players"])
        _v3_actor(row["victim"], payload["native_players"])
    data = payload["report_data"]
    _exact_fields(data, {"identity_source", "identity_status", "matched_rows", "total_rows"})
    if data["identity_source"] not in ("native_stats_history", "unavailable"):
        raise ReceiverError("invalid_report_identity_source")
    if data["identity_status"] not in ("complete", "partial", "unavailable", "ambiguous"):
        raise ReceiverError("invalid_report_identity_status")
    if (_integer(data["total_rows"], MAX_ROWS) != len(rows)
            or _integer(data["matched_rows"], MAX_ROWS) > len(rows)):
        raise ReceiverError("invalid_report_identity_counts")
    actual_matched = sum(all(a["identity_status"] == "resolved_native_stats" for a in (row["killer"], row["victim"])) for row in rows)
    states = {a["identity_status"] for row in rows for a in (row["killer"], row["victim"])}
    if data["matched_rows"] != actual_matched:
        raise ReceiverError("invalid_report_identity_counts")
    if data["identity_source"] == "unavailable":
        if data["identity_status"] != "unavailable" or states - {"unresolved"} or data["matched_rows"]:
            raise ReceiverError("invalid_report_identity_source")
    else:
        expected = ("complete" if actual_matched == len(rows) else "ambiguous" if "ambiguous" in states else
                    "partial" if "resolved_native_stats" in states else "unavailable")
        if data["identity_status"] != expected:
            raise ReceiverError("invalid_report_identity_status")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ReceiverError("invalid_submission") from None
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ReceiverError("payload_too_large", 413)
    return encoded


def _scalar(value):
    if isinstance(value, str):
        _text(value, 512)
    elif value is None or isinstance(value, bool):
        return
    elif isinstance(value, int):
        if not -(2**63) <= value <= 2**64 - 1:
            raise ReceiverError("invalid_display_integer")
    elif not isinstance(value, float) or not math.isfinite(value):
        raise ReceiverError("invalid_display_value")


def _field_name(key):
    _text(key, 64, nonempty=True)
    if _PRIVATE_FIELD.search(key):
        raise ReceiverError("private_runtime_field_forbidden")


def _display_fields(fields):
    if not isinstance(fields, dict) or len(fields) > 32:
        raise ReceiverError("invalid_display_fields")
    for key, value in fields.items():
        _field_name(key)
        _scalar(value)


class ArchiveIndex:
    """Metadata association against existing reports, opened strictly read-only."""

    def __init__(self, path, *, server_sessions=None, require_live_observation=False):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise ValueError("archive_database_missing")
        self.native_sessions = ServerSessionIndex(self.path, ledger=server_sessions,
                                                  require_live_observation=require_live_observation)

    def __call__(self, payload, *, replay_session_fallback=True):
        if payload.get("schema") not in SUPPORTED_SCHEMAS:
            raise ReceiverError("unsupported_schema")
        if payload.get("binding_mode") == "native_session":
            if _native_roster(payload["native_players"]) is None:
                return {"pending_reason": "native_roster_incomplete"}
            return self.native_sessions(payload)
        if payload.get("binding_mode") == "replay_sha256":
            return self._replay(payload, session_fallback=replay_session_fallback)
        raise ReceiverError("invalid_binding_mode")

    def _replay(self, payload, *, session_fallback=True):
        """Prefer exact bytes, otherwise verify a unique server-owned session.

        Client and server recordings can have different trailers. A missing
        file hash can therefore use the full native session metadata against a
        trusted, finished server archive; a bare session or header hash cannot.
        An existing but contradictory exact-hash row remains pending.
        """
        native_roster = _native_roster(payload["native_players"])
        if native_roster is None:
            return {"pending_reason": "native_roster_incomplete"}
        sha = payload["client_replay_sha256"]
        with closing(open_readonly_database(self.path)) as db:
            db.execute("PRAGMA query_only=ON")
            # Real ReplayStore databases have completion_status. Keeping the
            # payload check as well detects incomplete/corrupt archive rows.
            columns = {row[1] for row in db.execute("PRAGMA table_info(reports)")}
            completion = ",completion_status" if "completion_status" in columns else ""
            row = db.execute("SELECT sha256,payload" + completion + " FROM reports WHERE sha256=?", (sha,)).fetchone()
            if row is None:
                if not completion or not session_fallback:
                    return {"pending_reason": "replay_sha256_not_analyzed"}
                result = self.native_sessions(payload)
                if result.get("server_sha256"):
                    return result
                if result.get("pending_reason") == "server_session_not_finished_or_unmatched":
                    return {"pending_reason": "replay_sha256_not_analyzed"}
                return result
            try:
                if len(row[1]) > 64 * 1024 * 1024:
                    raise ValueError
                report = json.loads(row[1])
                match = report["match"]
                if (type(report.get("schema")) is not int or report["schema"] != 1
                        or report["source"].get("sha256") != sha):
                    return {"pending_reason": "archive_replay_invalid_report"}
                if (match.get("completion", {}).get("status") != "finished"
                        or (completion and row[2] != "finished")):
                    return {"pending_reason": "archive_replay_not_finished"}
                if (match.get("session_id") != payload["session_id"]
                        or match.get("map") != payload["map"]
                        or match.get("version") != payload["game_build"]
                        or type(match.get("version")) is not int):
                    return {"pending_reason": "archive_replay_metadata_mismatch"}
                if _archive_roster(report.get("players")) != native_roster:
                    return {"pending_reason": "archive_replay_roster_mismatch"}
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                return {"pending_reason": "archive_replay_invalid_report"}
            rooms = sorted({value[0] for value in db.execute(
                "SELECT server_id FROM report_sources WHERE sha256=? LIMIT 65", (sha,))
                if isinstance(value[0], str) and _ROOM.fullmatch(value[0])})
            if len(rooms) > 64:
                rooms = []
        return {"server_sha256": sha, "archive_rooms": rooms}


class CompositeArchiveIndex:
    """Search independent room archives without choosing the first match.

    All databases remain read-only. A known empty archive may precede its first
    collector migration; an unreadable archive or a conflicting relevant row
    keeps the submission pending even if another database contains a match.
    """

    def __init__(self, paths, *, server_sessions=None, require_live_observation=False):
        self.paths = tuple(dict.fromkeys(Path(path).resolve() for path in paths))
        if not 1 <= len(self.paths) <= 16:
            raise ValueError("invalid_archive_database_count")
        self.indexes = tuple(ArchiveIndex(path, server_sessions=server_sessions,
                                          require_live_observation=require_live_observation)
                             for path in self.paths)

    @staticmethod
    def _has_candidate(path, payload, *, exact_only=False):
        with closing(open_readonly_database(path)) as db:
            db.execute("PRAGMA query_only=ON")
            columns = {row[1] for row in db.execute("PRAGMA table_info(reports)")}
            if not {"sha256", "match_key", "payload"} <= columns:
                raise ValueError("invalid_archive_schema")
            if db.execute("SELECT 1 FROM reports LIMIT 1").fetchone() is None:
                return False
            source_columns = {row[1] for row in db.execute("PRAGMA table_info(report_sources)")}
            if not {"sha256", "server_id"} <= source_columns:
                raise ValueError("invalid_archive_schema")
            if payload["binding_mode"] == "replay_sha256":
                if db.execute("SELECT 1 FROM reports WHERE sha256=? LIMIT 1",
                              (payload["client_replay_sha256"],)).fetchone() is not None:
                    return True
                if exact_only or "completion_status" not in columns:
                    return False
            if payload["binding_mode"] == "native_session" and "completion_status" not in columns:
                raise ValueError("invalid_archive_schema")
            room = payload.get("room_hint")
            room_filter = " AND s.server_id=?" if room is not None else ""
            arguments = [payload["session_id"], payload["session_id"]]
            if room is not None:
                arguments.append(room)
            return db.execute("""SELECT 1 FROM reports r JOIN report_sources s ON s.sha256=r.sha256
                WHERE (r.match_key=? OR r.match_key='server:'||s.server_id||':'||?)"""
                              + room_filter + " LIMIT 1", arguments).fetchone() is not None

    def __call__(self, payload):
        matches = {}
        pending = []
        exact_indexes = []
        if payload["binding_mode"] == "replay_sha256":
            try:
                # Literal byte identity takes precedence across ALL rooms.
                # Validate every archive, even non-candidates, so an unreadable
                # room cannot hide another exact row with contradictory facts.
                exact_indexes = [index for index in self.indexes
                                 if self._has_candidate(index.path, payload, exact_only=True)]
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
                return {"pending_reason": "archive_temporarily_unavailable"}
        for index in exact_indexes or self.indexes:
            try:
                if not exact_indexes and not self._has_candidate(index.path, payload):
                    continue
                # If an exact row disappears after discovery, retain pending;
                # never replace that vanished evidence with a session match.
                result = index(payload, replay_session_fallback=not bool(exact_indexes))
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
                return {"pending_reason": "archive_temporarily_unavailable"}
            sha = result.get("server_sha256") if isinstance(result, dict) else None
            if not isinstance(sha, str) or not _HASH.fullmatch(sha):
                reason = result.get("pending_reason") if isinstance(result, dict) else None
                pending.append(reason or "archive_metadata_conflict")
                continue
            matches.setdefault(sha, []).append(result)
        if len(matches) > 1:
            return {"pending_reason": ("server_session_archive_ambiguous"
                                       if payload["binding_mode"] == "native_session" else "archive_match_ambiguous")}
        if pending:
            return {"pending_reason": sorted(set(pending))[0]}
        if not matches:
            reason = ("replay_sha256_not_analyzed" if payload["binding_mode"] == "replay_sha256" else
                      "server_session_not_finished_or_unmatched" if payload["binding_mode"] == "native_session" else
                      "archive_match_pending")
            return {"pending_reason": reason}
        sha, records = next(iter(matches.items()))
        maps = {record["archive_map"] for record in records if record.get("archive_map") is not None}
        evidence = {record["binding_evidence"] for record in records if record.get("binding_evidence") is not None}
        rooms = sorted({room for record in records for room in record.get("archive_rooms", [])})
        verified = [record for record in records if record.get("server_state_verified") is True]
        proofs = {(record.get("server_id"), record.get("server_epoch")) for record in verified}
        if len(maps) > 1 or len(evidence) > 1 or len(rooms) > 64:
            return {"pending_reason": "archive_metadata_conflict"}
        if len(proofs) > 1:
            return {"pending_reason": "server_live_state_ambiguous"}
        result = {"server_sha256": sha, "archive_rooms": rooms}
        if maps:
            result["archive_map"] = next(iter(maps))
        if evidence:
            result["binding_evidence"] = next(iter(evidence))
        if payload["binding_mode"] == "native_session":
            result["server_state_verified"] = bool(verified)
            if verified:
                for key in ("server_id", "server_epoch", "server_observations"):
                    result[key] = verified[0].get(key)
        return result


class ReceiverStore:
    def __init__(self, path, *, archive_index=None):
        self.path = Path(path).resolve()
        archive_paths = ((archive_index.path,) if isinstance(archive_index, ArchiveIndex) else
                         archive_index.paths if isinstance(archive_index, CompositeArchiveIndex) else ())
        if self.path in archive_paths:
            raise ValueError("receiver_database_must_be_separate_from_archive")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.archive_index = archive_index
        # A swallowed reconcile failure would strand every pending submission,
        # so the last error is retained for the status document.
        self.last_reconcile_error = None
        with self._connection() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reports'").fetchone():
                raise ValueError("receiver_database_must_be_separate_from_archive")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS submissions (
                submission_id TEXT PRIMARY KEY,
                installation_id TEXT NOT NULL,
                room_hint TEXT NOT NULL,
                session_id TEXT NOT NULL,
                game_build INTEGER NOT NULL,
                map TEXT NOT NULL,
                payload TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending_match','accepted')),
                server_sha256 TEXT,
                archive_rooms TEXT NOT NULL DEFAULT '[]',
                pending_reason TEXT,
                binding_evidence TEXT,
                server_state_verified INTEGER NOT NULL DEFAULT 0,
                received_at REAL NOT NULL,
                matched_at REAL,
                last_checked_at REAL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS submissions_match ON submissions(status,received_at)")

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def _binding(self, payload):
        empty = {"sha256": None, "rooms": [], "map": None, "evidence": None, "server_state_verified": False}
        if self.archive_index is None:
            return {**empty, "reason": "archive_not_configured"}
        try:
            result = self.archive_index(payload)
            sha = result.get("server_sha256") if isinstance(result, dict) else None
            if isinstance(sha, str) and _HASH.fullmatch(sha):
                rooms = result.get("archive_rooms", [])
                if (not isinstance(rooms, list) or len(rooms) > 64
                        or any(not isinstance(room, str) or not _ROOM.fullmatch(room) for room in rooms)):
                    rooms = []
                evidence = result.get("binding_evidence")
                if evidence not in ("server_finished_archive", "exact_replay_sha256_archive"):
                    evidence = "exact_replay_sha256_archive" if payload["binding_mode"] == "replay_sha256" else "server_finished_archive"
                archive_map = result.get("archive_map")
                if archive_map is not None:
                    _text(archive_map, 512, nonempty=True)
                return {"sha256": sha, "rooms": sorted(set(rooms)), "reason": None,
                        "map": archive_map, "evidence": evidence,
                        "server_state_verified": (payload["binding_mode"] == "native_session"
                            and payload.get("client_context_evidence", {}).get("native_source_kind") == "network_live"
                            and result.get("server_state_verified") is True)}
            reason = result.get("pending_reason") if isinstance(result, dict) else None
            if not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason):
                reason = "archive_match_pending"
            return {**empty, "reason": reason}
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            # A temporarily unavailable archive leaves a retained pending item.
            return {**empty, "reason": "archive_temporarily_unavailable"}

    @staticmethod
    def _room(payload, binding):
        if payload.get("room_hint") is not None:
            return payload["room_hint"]
        return binding["rooms"][0] if len(binding["rooms"]) == 1 else ""

    @staticmethod
    def _map(payload, binding):
        return payload.get("map") or binding.get("map") or ""

    @staticmethod
    def _native_receipt(receipt, record):
        result = {**receipt, "binding_evidence": record["binding_evidence"],
                  "server_state_verified": bool(record["server_state_verified"])}
        if record["status"] == "pending_match":
            result["pending_reason"] = record["pending_reason"]
        return result

    def submit(self, payload):
        encoded = validate_payload(payload)
        text = encoded.decode("utf-8")
        fingerprint = hashlib.sha256(encoded).hexdigest()
        now = time.time()
        binding = self._binding(payload)
        sha = binding["sha256"]
        rooms = json.dumps(binding["rooms"], separators=(",", ":"))
        room = self._room(payload, binding)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT payload,payload_sha256 FROM submissions WHERE submission_id=?",
                                  (payload["submission_id"],)).fetchone()
            if previous is not None:
                if previous["payload_sha256"] != fingerprint or previous["payload"] != text:
                    raise ReceiverError("submission_id_conflict", 409)
                if sha:
                    db.execute("""UPDATE submissions SET status='accepted',server_sha256=?,matched_at=?,
                        room_hint=?,archive_rooms=?,map=?,binding_evidence=?,server_state_verified=?,pending_reason=NULL,last_checked_at=?
                        WHERE submission_id=? AND status='pending_match'""",
                               (sha, now, room, rooms, self._map(payload, binding), binding["evidence"],
                                int(binding["server_state_verified"]), now, payload["submission_id"]))
                else:
                    db.execute("""UPDATE submissions SET pending_reason=?,last_checked_at=?
                        WHERE submission_id=? AND status='pending_match'""",
                               (binding["reason"], now, payload["submission_id"]))
                receipt = {"submission_id": payload["submission_id"], "status": "duplicate"}
                if payload["binding_mode"] == "native_session":
                    record = db.execute("SELECT status,pending_reason,binding_evidence,server_state_verified FROM submissions WHERE submission_id=?",
                                        (payload["submission_id"],)).fetchone()
                    return self._native_receipt(receipt, record)
                return receipt
            state = "accepted" if sha else "pending_match"
            db.execute("""INSERT INTO submissions
                (submission_id,installation_id,room_hint,session_id,game_build,map,payload,
                 payload_sha256,status,server_sha256,archive_rooms,pending_reason,binding_evidence,server_state_verified,
                 received_at,matched_at,last_checked_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                payload["submission_id"], payload["installation_id"], room, payload["session_id"],
                payload["game_build"], self._map(payload, binding), text, fingerprint, state, sha, rooms, binding["reason"],
                binding["evidence"], int(binding["server_state_verified"]), now,
                now if sha else None, now))
        receipt = {"submission_id": payload["submission_id"], "status": state}
        if payload["binding_mode"] == "native_session":
            return self._native_receipt(receipt, {"status": state, "pending_reason": binding["reason"],
                "binding_evidence": binding["evidence"], "server_state_verified": binding["server_state_verified"]})
        if payload["binding_mode"] == "replay_sha256" and not sha:
            receipt["pending_reason"] = binding["reason"]
        return receipt

    def _stored_payload(self, submission_id):
        """Read one stored payload, retrying a transient storage failure.

        A large payload read can fail once with ``disk I/O error`` while the
        inbox WAL is being checkpointed or unlinked by another connection. One
        such failure must not abandon the whole reconciliation batch.
        """
        for attempt in range(3):
            try:
                with self._connection() as db:
                    row = db.execute("SELECT payload FROM submissions WHERE submission_id=?",
                                     (submission_id,)).fetchone()
                return json.loads(row["payload"]) if row is not None else None
            except (sqlite3.Error, ValueError, TypeError, RecursionError):
                if attempt == 2:
                    return None
                time.sleep(0.2 * (attempt + 1))
        return None

    def reconcile(self, limit=100):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("invalid_reconcile_limit")
        if self.archive_index is None:
            return {"checked": 0, "accepted": 0}
        with self._connection() as db:
            submission_ids = [row["submission_id"] for row in db.execute(
                "SELECT submission_id FROM submissions WHERE status='pending_match' "
                "ORDER BY COALESCE(last_checked_at,received_at),received_at LIMIT ?", (limit,))]
        accepted = checked = 0
        for submission_id in submission_ids:
            payload = self._stored_payload(submission_id)
            if payload is None:
                continue
            checked += 1
            binding = self._binding(payload)
            try:
                with self._connection() as db:
                    now = time.time()
                    db.execute("UPDATE submissions SET last_checked_at=?,pending_reason=? WHERE submission_id=?",
                               (now, binding["reason"], submission_id))
                    if binding["sha256"]:
                        cursor = db.execute("""UPDATE submissions SET status='accepted',server_sha256=?,matched_at=?,
                            room_hint=?,archive_rooms=?,map=?,binding_evidence=?,server_state_verified=?,pending_reason=NULL
                            WHERE submission_id=? AND status='pending_match'""",
                                            (binding["sha256"], now, self._room(payload, binding),
                                             json.dumps(binding["rooms"], separators=(",", ":")), self._map(payload, binding),
                                             binding["evidence"], int(binding["server_state_verified"]), submission_id))
                        accepted += cursor.rowcount
            except sqlite3.Error:
                continue
        return {"checked": checked, "accepted": accepted}

    def status(self):
        counts = {"pending_match": 0, "accepted": 0}
        reasons = {}
        with self._connection() as db:
            for row in db.execute("SELECT status,COUNT(*) AS n FROM submissions GROUP BY status"):
                counts[row["status"]] = row["n"]
            for row in db.execute("SELECT pending_reason,COUNT(*) AS n FROM submissions WHERE status='pending_match' GROUP BY pending_reason"):
                reasons[row["pending_reason"] or "archive_match_pending"] = row["n"]
        counts["total"] = counts["pending_match"] + counts["accepted"]
        return {"service": "sd2-companion-receiver", "archive_configured": self.archive_index is not None,
                "submissions": counts, "pending_reasons": reasons,
                "last_reconcile_error": self.last_reconcile_error}

    def report(self, submission_id):
        if not isinstance(submission_id, str) or not _HASH.fullmatch(submission_id):
            raise ReceiverError("invalid_submission_id")
        with self._connection() as db:
            row = db.execute("SELECT * FROM submissions WHERE submission_id=?", (submission_id,)).fetchone()
        if row is None:
            return None
        return {"submission_id": submission_id, "status": row["status"], "server_sha256": row["server_sha256"],
                "archive_rooms": json.loads(row["archive_rooms"]), "pending_reason": row["pending_reason"],
                "binding_evidence": row["binding_evidence"], "server_state_verified": bool(row["server_state_verified"]),
                "report": json.loads(row["payload"])}

    def receipt(self, submission_id, *, installation_id=None):
        """Return owned delivery state only; never return report/player data."""
        if not isinstance(submission_id, str) or not _HASH.fullmatch(submission_id):
            raise ReceiverError("invalid_submission_id")
        with self._connection() as db:
            row = db.execute("SELECT installation_id,status,pending_reason FROM submissions WHERE submission_id=?",
                             (submission_id,)).fetchone()
        if row is None or installation_id is not None and row["installation_id"] != installation_id:
            return None
        result = {"submission_id": submission_id, "status": row["status"]}
        if row["status"] == "pending_match":
            reason = row["pending_reason"]
            if isinstance(reason, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason):
                result["pending_reason"] = reason
        return result


class CatalogSource:
    """Server-owned room table served to companions; the operator edits one file.

    Stable ETags permit 304 until a file edit or the refresh window. Response
    lifetime starts when it is issued, never at the configuration file's mtime.
    Nothing here reads or writes client state.
    """

    def __init__(self, path, *, interval_seconds=CATALOG_DEFAULT_INTERVAL, clock=time.time):
        self.path = Path(path).resolve()
        self.interval = float(interval_seconds)
        self._clock = clock
        self._stamp = None
        self._body = None
        self._etag = None
        self._refresh_at = 0.0

    def _read(self):
        from .config import ConfigError, parse_rooms
        from .catalog import TUNABLES
        try:
            info = self.path.stat()
        except OSError:
            raise ReceiverError("catalog_unavailable", 503) from None
        if info.st_size > CATALOG_MAX_BYTES:
            raise ReceiverError("catalog_too_large", 503)
        try:
            raw = self.path.read_bytes()
            document = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeError):
            raise ReceiverError("catalog_unreadable", 503) from None
        if not isinstance(document, dict) or document.get("schema") != 1:
            raise ReceiverError("catalog_invalid", 503)
        try:
            rooms = parse_rooms(document.get("rooms"))
        except ConfigError:
            raise ReceiverError("catalog_invalid", 503) from None
        if not rooms:
            raise ReceiverError("catalog_without_rooms", 503)
        settings = document.get("settings", {})
        if not isinstance(settings, dict) or set(settings) - set(TUNABLES):
            raise ReceiverError("catalog_invalid", 503)
        for key, value in settings.items():
            low, high = TUNABLES[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise ReceiverError("catalog_invalid", 503)
        after = self.path.stat()
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ReceiverError("catalog_changed_while_reading", 503)
        issued_at = self._clock()
        generated = datetime.datetime.fromtimestamp(issued_at, datetime.timezone.utc)
        expires = datetime.datetime.fromtimestamp(issued_at + self.interval, datetime.timezone.utc)
        body = {"schema": 1, "generated_at": generated.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "rooms": [{"id": room.id,
                           "endpoints": [{"host": endpoint.host, "port": endpoint.port} for endpoint in room.endpoints],
                           "server_names": list(room.server_names)} for room in rooms],
                "settings": {key: float(value) for key, value in settings.items()}}
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":"), sort_keys=True).encode("utf-8")
        self._refresh_at = issued_at + self.interval / 2
        return (info.st_mtime_ns, info.st_size), encoded, '"' + hashlib.sha256(encoded).hexdigest() + '"'

    def document(self):
        """Return ``(body, etag)``; raises :class:`ReceiverError` when unusable."""
        try:
            stamp = (self.path.stat().st_mtime_ns, self.path.stat().st_size)
        except OSError:
            raise ReceiverError("catalog_unavailable", 503) from None
        if self._body is None or stamp != self._stamp or self._clock() >= self._refresh_at:
            self._stamp, self._body, self._etag = self._read()
        return self._body, self._etag

    def status(self):
        try:
            self.document()
        except ReceiverError as exc:
            return {"configured": True, "available": False, "error": exc.code}
        try:
            document = json.loads(self._body.decode("utf-8"))
        except ValueError:
            return {"configured": True, "available": False, "error": "catalog_invalid"}
        return {"configured": True, "available": True, "room_count": len(document["rooms"]),
                "generated_at": document["generated_at"], "expires_at": document["expires_at"]}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def _authorized(self, *, allow_installation=False):
        auth = self.headers.get_all("Authorization", [])
        incoming = auth[0].encode("utf-8") if len(auth) == 1 else b""
        self.installation_id = None
        if len(incoming) > 8192:
            return False
        if hmac.compare_digest(incoming, self.server.authorization):
            return True
        if allow_installation and self.server.installations is not None and incoming.startswith(b"Bearer "):
            self.installation_id = self.server.installations.authenticate(incoming[7:].decode("utf-8"))
            return self.installation_id is not None
        return False

    def _drain_bounded_body(self):
        # Finish a small, declared body before closing an early rejection.
        # Otherwise Windows can reset the socket before the client reads 401.
        if self.command != "POST" or getattr(self, "_body_consumed", False):
            return
        self._body_consumed = True
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1
                or not re.fullmatch(r"[0-9]{1,10}", lengths[0])):
            self._drain_available_body()
            return
        remaining = int(lengths[0])
        if not 0 < remaining <= MAX_PAYLOAD_BYTES:
            self._drain_available_body()
            return
        deadline = time.monotonic() + 15
        try:
            while remaining and time.monotonic() < deadline:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
        except OSError:
            pass

    def _drain_available_body(self):
        # A malformed request has no trustworthy body boundary. Briefly drain
        # only bytes already arriving; leaving them unread can reset Windows'
        # connection before the peer receives its 4xx response.
        original_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(0.02)
            for _ in range(3):
                if not self.rfile.read1(65536):
                    break
        except OSError:
            pass
        finally:
            self.connection.settimeout(original_timeout)

    def _reply(self, status, payload):
        self._drain_bounded_body()
        encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        if status == 429:
            self.send_header("Retry-After", "60")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(encoded)

    def do_GET(self):
        try:
            if self.path == "/health":
                status = self.server.store.status()
                status["protocol"] = TASK_SCHEMA
                status["combat"] = self.server.combat_store.status() if self.server.combat_store is not None else {"available": False}
                status["catalog"] = self.server.catalog.status() if self.server.catalog is not None else {"configured": False}
                self._reply(200, status)
                return
            if self.path in (CATALOG_PATH, ROOMS_PATH):
                if not self._authorized(allow_installation=True):
                    self._reply(401, {"error": "unauthorized"})
                    return
                self._catalog()
                return
            receipt = re.fullmatch(re.escape(RECEIPTS_PATH) + r"/([0-9a-f]{64})", self.path)
            if receipt:
                if not self._authorized(allow_installation=True):
                    self._reply(401, {"error": "unauthorized"})
                    return
                result = self.server.store.receipt(receipt[1], installation_id=self.installation_id)
                self._reply(200 if result is not None else 404, result or {"error": "not_found"})
                return
            raw_report = re.fullmatch(re.escape(API_PATH) + r"/([0-9a-f]{64})", self.path)
            player = re.fullmatch(re.escape(COMBAT_PATH) + r"/players/([0-9]{1,20})", self.path)
            match = re.fullmatch(re.escape(COMBAT_PATH) + r"/matches/([0-9a-f]{64})", self.path)
            if not (raw_report or player or match):
                self._reply(404, {"error": "not_found"})
                return
            if not self._authorized():
                self._reply(401, {"error": "unauthorized"})
                return
            if raw_report:
                result = self.server.store.report(raw_report[1])
                self._reply(200 if result is not None else 404, result or {"error": "not_found"})
            elif self.server.combat_store is None:
                self._reply(503, {"error": "combat_not_configured"})
            else:
                result = self.server.combat_store.player(player[1]) if player else self.server.combat_store.match(match[1])
                self._reply(200, result)
        except (OSError, sqlite3.Error):
            self._reply(503, {"error": "storage_unavailable"})
        except ValueError:
            self._reply(400, {"error": "invalid_query"})

    def _catalog(self):
        """Serve the server-owned room table; a matching ETag answers 304."""
        source = self.server.catalog
        if source is None:
            self._reply(503, {"error": "catalog_not_configured"})
            return
        try:
            body, etag = source.document()
        except ReceiverError as exc:
            self._reply(exc.status_code, {"error": exc.code})
            return
        current = self.headers.get_all("If-None-Match", [])
        headers = [("ETag", etag), ("Cache-Control", "no-store")]
        if len(current) == 1 and current[0] == etag:
            self.send_response(304)
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.close_connection = True
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_POST(self):
        if self.path == INSTALLATIONS_PATH:
            self._register_installation()
            return
        if self.path != API_PATH:
            self._reply(404, {"error": "not_found"})
            return
        try:
            authorized = self._authorized(allow_installation=True)
        except (OSError, sqlite3.Error):
            self._reply(503, {"error": "receiver_unavailable"})
            return
        if not authorized:
            self._reply(401, {"error": "unauthorized"})
            return
        try:
            if self.headers.get("Transfer-Encoding") is not None:
                raise ReceiverError("transfer_encoding_unsupported")
            if self.headers.get("Expect") is not None:
                raise ReceiverError("expectation_unsupported", 417)
            lengths = self.headers.get_all("Content-Length", [])
            if not lengths:
                raise ReceiverError("content_length_required", 411)
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
                raise ReceiverError("invalid_content_length")
            length = int(lengths[0])
            if not 0 < length <= MAX_PAYLOAD_BYTES:
                raise ReceiverError("payload_too_large", 413)
            if self.headers.get_content_type() != "application/json":
                raise ReceiverError("json_content_type_required", 415)
            self._body_consumed = True
            data = self.rfile.read(length)
            if len(data) != length:
                raise ReceiverError("incomplete_body")
            payload = decode_payload(data)
            key = self.headers.get_all("Idempotency-Key", [])
            if len(key) != 1 or not isinstance(payload, dict) or key[0] != payload.get("submission_id"):
                raise ReceiverError("idempotency_key_mismatch")
            if self.installation_id is not None:
                validate_payload(payload)
                if payload["installation_id"] != self.installation_id:
                    raise ReceiverError("installation_credential_scope_mismatch", 403)
                self.server.installations.check_submission(self.installation_id, payload["submission_id"])
            receipt = self.server.store.submit(payload)
            self._reply(202 if receipt["status"] == "pending_match" else 200, receipt)
        except (ReceiverError, EnrollmentError) as exc:
            self._reply(exc.status_code, {"error": exc.code})
        except (OSError, sqlite3.Error):
            self._reply(503, {"error": "receiver_unavailable"})

    def _register_installation(self):
        registry = self.server.installations
        if registry is None:
            self._reply(404, {"error": "not_found"})
            return
        try:
            if self.headers.get("Transfer-Encoding") is not None or self.headers.get("Expect") is not None:
                raise ReceiverError("invalid_registration_request")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
                raise ReceiverError("invalid_content_length")
            length = int(lengths[0])
            if not 0 < length <= MAX_REGISTRATION_BYTES:
                raise ReceiverError("registration_too_large", 413)
            if self.headers.get_content_type() != "application/json":
                raise ReceiverError("json_content_type_required", 415)
            self._body_consumed = True
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ReceiverError("incomplete_body")
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_nonfinite)
            forwarded = self.headers.get_all("X-Real-IP", [])
            address = client_address(self.client_address[0], forwarded[0] if len(forwarded) == 1 else None)
            created = registry.register(value, source=address)
            self._reply(201 if created else 200, {"schema": 1, "installation_id": value["installation_id"],
                                                "status": "registered"})
        except (ReceiverError, EnrollmentError) as exc:
            self._reply(exc.status_code, {"error": exc.code})
        except (UnicodeError, ValueError, RecursionError):
            self._reply(400, {"error": "invalid_registration_json"})
        except (OSError, sqlite3.Error):
            self._reply(503, {"error": "registration_unavailable"})


class ReceiverServer(ThreadingHTTPServer):
    daemon_threads = True
    reconcile_interval = 30

    def __init__(self, address, store, token, combat_store=None, catalog=None, installations=None):
        self.store = store
        self.combat_store = combat_store
        self.catalog = catalog
        self.installations = installations
        self.authorization = ("Bearer " + token).encode("ascii")
        self._last_reconcile = 0.0
        self._inputs_signature = None
        super().__init__(address, _Handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(15)
        return connection, address

    def handle_error(self, request, client_address):
        # Default HTTPServer tracebacks may include untrusted request content.
        # Individual requests get no body/token/name logging.
        pass

    def _archive_paths(self):
        index = self.store.archive_index
        return index.paths if isinstance(index, CompositeArchiveIndex) else (index.path,)

    def service_actions(self):
        if self.store.archive_index is None:
            return
        if time.monotonic() - self._last_reconcile < self.reconcile_interval:
            return
        self._last_reconcile = time.monotonic()
        paths = self._archive_paths()
        signature = None
        if self.combat_store is not None and not self._ledger_bound():
            try:
                signature = self.combat_store.inputs_signature(self.store.path, paths)
            except (OSError, sqlite3.Error, ValueError):
                signature = None
            if signature is not None and signature == self._inputs_signature:
                # Nothing arrived and no archive or catalog revision changed:
                # an idle inbox must not re-read stored payloads every interval.
                return
        try:
            self.store.reconcile()
            self.store.last_reconcile_error = None
            if self.combat_store is not None:
                # Reconciliation can accept a retained report without any
                # archive change (for example after repairing the matcher).
                # Its post-reconcile state must invalidate the derived cache.
                signature = self.combat_store.inputs_signature(self.store.path, paths)
                self.combat_store.refresh(self.store.path, paths, signature=signature)
                # Do not mark arrivals during the scan as processed. They must
                # make the next cycle run even if this refresh succeeded.
                self._inputs_signature = signature
        except (OSError, sqlite3.Error, ValueError) as error:
            # Keep serving, but never hide a stalled backlog: the status
            # endpoint reports the last failure instead of swallowing it.
            self.store.last_reconcile_error = type(error).__name__

    def _ledger_bound(self):
        """A live session ledger changes outside the inbox and archive databases."""
        return getattr(getattr(self.store.archive_index, "native_sessions", None), "ledger", None) is not None


def create_server(store, token, *, host="127.0.0.1", port=0, combat_store=None, catalog=None, installations=None):
    if (not isinstance(token, str) or not 1 <= len(token) <= 4096
            or any(not 33 <= ord(char) <= 126 for char in token)):
        raise ValueError("invalid_receiver_token")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("invalid_receiver_port")
    return ReceiverServer((host, port), store, token, combat_store=combat_store, catalog=catalog,
                          installations=installations)
