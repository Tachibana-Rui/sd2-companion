"""Server-owned session observations and exact native-session association.

The existing RCON ``display_all_clients``/settings snapshot does not expose a
GameSetup UniqueSessionId or a game build. Those must come from the server's
native GameSetup, its own replay header, or a completed server replay archive.
This module never invents a game session ID from a roster, time, or log phase.
No network access occurs here; all state producers are injected by the host.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import uuid

from . import open_readonly_database


_ROOM = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"(?:0|[1-9][0-9]{0,19})\Z")
PHASES = frozenset((0, 50, 51, 52, 53, 101))
IDENTITY_SOURCES = frozenset(("server_native_game_setup", "server_replay_header"))


class ServerSessionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _text(value, limit=128):
    if (not isinstance(value, str) or not 0 < len(value) <= limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ServerSessionError("invalid_server_session_text")
    return value


def _server(value):
    if not isinstance(value, str) or not _ROOM.fullmatch(value):
        raise ServerSessionError("invalid_server_identity")
    return value


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ServerSessionError("invalid_server_observation_time")
    return float(value)


def _build(value):
    if type(value) is not int or not 1 <= value < 2**31:
        raise ServerSessionError("invalid_server_game_build")
    return value


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value) or int(value) >= 2**64 - 1:
        raise ServerSessionError("invalid_server_player_id")
    return value


def _roster(players, *, native=False):
    if not isinstance(players, (list, tuple)) or not 1 <= len(players) <= 64:
        raise ServerSessionError("invalid_server_session_roster")
    rows = []
    seen = set()
    for player in players:
        if not isinstance(player, dict):
            raise ServerSessionError("invalid_server_session_roster")
        if "is_observer" in player and type(player["is_observer"]) is not bool:
            raise ServerSessionError("invalid_server_session_roster")
        if player.get("is_observer") is True:
            continue
        identifier = _identifier(player.get("player_user_id" if native else "id"))
        alliance = player.get("alliance")
        if type(alliance) is not int or alliance not in (0, 1) or identifier in seen:
            raise ServerSessionError("invalid_server_session_roster")
        seen.add(identifier)
        rows.append((identifier, alliance))
    if not rows:
        raise ServerSessionError("invalid_server_session_roster")
    return tuple(sorted(rows))


@dataclass(frozen=True)
class ServerObservation:
    server_id: str
    server_epoch: str
    session_id: str
    game_state: int
    map: str
    game_build: int
    participants: tuple[tuple[str, int], ...]
    observed_at: float
    sequence: int
    identity_source: str

    def metadata(self):
        return {"map": self.map, "game_build": self.game_build,
                "participants": [{"id": identifier, "alliance": alliance}
                                 for identifier, alliance in self.participants]}


def adapt_raw_server_state(raw, *, server_id, server_epoch, session_identity,
                           online_player_ids, observed_at, sequence):
    """Adapt the existing host's raw LocalTransport.read_state() shape.

    Use raw transport state, not HostedManager's public settings view: the
    latter currently omits GameState and per-player RequestState/Alliance.
    ``session_identity`` is a server-owned GameSetup/header observation. Its
    participants are the complete round roster, including players who later
    disconnected; currently online participant details only corroborate it.
    """
    _server(server_id)
    _text(server_epoch)
    if (not isinstance(raw, dict) or raw.get("partial") is not False
            or not isinstance(raw.get("settings_scan"), dict)
            or raw["settings_scan"].get("complete") is not True):
        raise ServerSessionError("server_snapshot_incomplete")
    if type(sequence) is not int or not 0 <= sequence < 2**63:
        raise ServerSessionError("invalid_server_observation_sequence")
    observed_at = _number(observed_at)
    variables, sources = raw.get("variables"), raw.get("sources")
    if not isinstance(variables, dict) or not isinstance(sources, dict):
        raise ServerSessionError("server_snapshot_incomplete")
    # variables.ini contains startup settings; only confirmed native log state
    # is accepted as an observation of the running round.
    if sources.get("GameState") != "serverlog.txt" or sources.get("Map") != "serverlog.txt":
        raise ServerSessionError("server_phase_not_observed")
    phase = variables.get("GameState")
    if not isinstance(phase, str) or phase not in {str(value) for value in PHASES}:
        raise ServerSessionError("unknown_server_phase")
    if not isinstance(session_identity, dict) or session_identity.get("source") not in IDENTITY_SOURCES:
        raise ServerSessionError("server_session_identity_unavailable")
    session = _text(session_identity.get("session_id"))
    map_id = _text(session_identity.get("map"), 512)
    build = _build(session_identity.get("game_build"))
    roster = _roster(session_identity.get("participants"))
    if variables.get("Map") != map_id:
        raise ServerSessionError("server_session_map_mismatch")
    if not isinstance(online_player_ids, (list, tuple, set)) or len(online_player_ids) > 256:
        raise ServerSessionError("server_online_roster_unavailable")
    online = {_identifier(value) for value in online_player_ids}
    details = raw.get("players")
    if not isinstance(details, dict):
        raise ServerSessionError("server_player_details_unavailable")
    sides = dict(roster)
    for identifier in online:
        row = details.get(identifier)
        if not isinstance(row, dict):
            raise ServerSessionError("server_player_details_unavailable")
        if row.get("RequestState") not in ("51", "52", "53", "101"):
            continue  # Connected outside-lobby players are not participants.
        if row.get("PlayerAlliance") not in ("0", "1"):
            raise ServerSessionError("server_player_alliance_unavailable")
        if sides.get(identifier) != int(row["PlayerAlliance"]):
            raise ServerSessionError("server_session_roster_mismatch")
    return ServerObservation(server_id, server_epoch, session, int(phase), map_id,
                             build, roster, observed_at, sequence, session_identity["source"])


class ServerSessionLedger:
    """Persist only server-observed 53 -> 101 completion transitions.

    server_epoch is a host/container run identifier, not a GameSetup session.
    Its source can be the existing game.generation.json marker. No game phase
    or identity is read from a client report in this class.
    """
    def __init__(self, path, *, clock=time.time, max_age=5, max_gap=15, retention=86400):
        self.path = Path(path).resolve()
        self.clock = clock
        self.max_age, self.max_gap, self.retention = map(_number, (max_age, max_gap, retention))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name IN ('reports','submissions') LIMIT 1").fetchone():
                raise ServerSessionError("session_ledger_must_be_separate_database")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS server_session_heads (
                    server_id TEXT PRIMARY KEY, server_epoch TEXT NOT NULL,
                    sequence INTEGER NOT NULL, phase INTEGER NOT NULL,
                    session_id TEXT NOT NULL, observed_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS server_sessions (
                    server_id TEXT NOT NULL, server_epoch TEXT NOT NULL, session_id TEXT NOT NULL,
                    server_round_id TEXT NOT NULL, state TEXT NOT NULL,
                    metadata TEXT NOT NULL, identity_source TEXT NOT NULL,
                    started_at REAL NOT NULL, observed_at REAL NOT NULL, finished_at REAL,
                    PRIMARY KEY(server_id,server_epoch,session_id));
                CREATE INDEX IF NOT EXISTS server_sessions_identity
                    ON server_sessions(server_id,session_id,state);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def observe(self, observation):
        if not isinstance(observation, ServerObservation):
            raise ServerSessionError("invalid_server_observation")
        _server(observation.server_id);_text(observation.server_epoch);_text(observation.session_id)
        _text(observation.map, 512);_build(observation.game_build);_number(observation.observed_at)
        if (type(observation.game_state) is not int or observation.game_state not in PHASES
                or type(observation.sequence) is not int or not 0 <= observation.sequence < 2**63
                or observation.identity_source not in IDENTITY_SOURCES):
            raise ServerSessionError("invalid_server_observation")
        try:
            participants = _roster([{"id": row[0], "alliance": row[1]} for row in observation.participants])
        except (TypeError, IndexError):
            raise ServerSessionError("invalid_server_session_roster") from None
        if participants != observation.participants:
            raise ServerSessionError("invalid_server_session_roster")
        now = _number(self.clock())
        if observation.observed_at > now + 1 or now - observation.observed_at > self.max_age:
            raise ServerSessionError("server_snapshot_stale")
        metadata = json.dumps(observation.metadata(), sort_keys=True, separators=(",", ":"))
        identity = (observation.server_id, observation.server_epoch, observation.session_id)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            head = db.execute("SELECT * FROM server_session_heads WHERE server_id=?", (observation.server_id,)).fetchone()
            same_epoch = head is not None and head["server_epoch"] == observation.server_epoch
            if same_epoch and observation.sequence <= head["sequence"]:
                return False
            contiguous = (same_epoch and observation.observed_at >= head["observed_at"]
                          and observation.observed_at - head["observed_at"] <= self.max_gap)
            same_round = contiguous and head["session_id"] == observation.session_id
            if head is not None and (not same_round or observation.game_state not in (53, 101)):
                db.execute("""UPDATE server_sessions SET state='interrupted',finished_at=NULL
                    WHERE server_id=? AND server_epoch=? AND session_id=? AND state='running'""",
                           (head["server_id"], head["server_epoch"], head["session_id"]))
            row = db.execute("SELECT * FROM server_sessions WHERE server_id=? AND server_epoch=? AND session_id=?", identity).fetchone()
            if row is not None and row["metadata"] != metadata:
                db.execute("UPDATE server_sessions SET state='interrupted',finished_at=NULL WHERE server_id=? AND server_epoch=? AND session_id=?", identity)
            elif observation.game_state == 53:
                if row is None:
                    state = "interrupted" if same_round and head["phase"] == 101 else "running"
                    db.execute("INSERT INTO server_sessions VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                               (*identity, str(uuid.uuid4()), state, metadata, observation.identity_source,
                                observation.observed_at, observation.observed_at))
                elif row["state"] == "finished":
                    # Reusing a session ID cannot silently reattach a new game
                    # to the previous completion, even with an equal roster.
                    db.execute("UPDATE server_sessions SET state='interrupted',finished_at=NULL WHERE server_id=? AND server_epoch=? AND session_id=?", identity)
                else:
                    db.execute("UPDATE server_sessions SET observed_at=? WHERE server_id=? AND server_epoch=? AND session_id=?",
                               (observation.observed_at, *identity))
            elif observation.game_state == 101 and row is not None and row["state"] == "running":
                if same_round and head["phase"] == 53:
                    db.execute("""UPDATE server_sessions SET state='finished',observed_at=?,finished_at=?
                        WHERE server_id=? AND server_epoch=? AND session_id=?""",
                               (observation.observed_at, observation.observed_at, *identity))
                else:
                    db.execute("UPDATE server_sessions SET state='interrupted',finished_at=NULL WHERE server_id=? AND server_epoch=? AND session_id=?", identity)
            db.execute("""INSERT INTO server_session_heads VALUES(?,?,?,?,?,?)
                ON CONFLICT(server_id) DO UPDATE SET server_epoch=excluded.server_epoch,
                sequence=excluded.sequence,phase=excluded.phase,session_id=excluded.session_id,
                observed_at=excluded.observed_at""", (observation.server_id, observation.server_epoch,
                observation.sequence, observation.game_state, observation.session_id, observation.observed_at))
        return True

    def invalidate(self, server_id):
        with self._db() as db:
            db.execute("UPDATE server_sessions SET state='interrupted',finished_at=NULL WHERE server_id=? AND state='running'", (_server(server_id),))
            db.execute("DELETE FROM server_session_heads WHERE server_id=?", (server_id,))

    def session_evidence(self, server_id, session_id, *, server_epoch=None):
        """Distinguish absent evidence from authoritative contradictory state.

        An epoch may be supplied explicitly, or derived only when this exact
        server/session has one record. No newest-record or time matching is
        used to choose between server generations.
        """
        _server(server_id);_text(session_id)
        args = [server_id, session_id]
        query = "SELECT * FROM server_sessions WHERE server_id=? AND session_id=?"
        if server_epoch is not None:
            query += " AND server_epoch=?";args.append(_text(server_epoch))
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            rows = db.execute(query + " LIMIT 2", args).fetchall()
            if not rows and server_epoch is not None and db.execute(
                    "SELECT 1 FROM server_sessions WHERE server_id=? AND session_id=? LIMIT 1",
                    (server_id, session_id)).fetchone():
                return {"status": "epoch_mismatch", "reason": "server_live_epoch_mismatch"}
        if not rows:
            return {"status": "missing"}
        if len(rows) != 1:
            return {"status": "ambiguous", "reason": "server_live_state_ambiguous"}
        result = dict(rows[0])
        if result["state"] != "finished":
            reason = "server_live_session_not_finished" if result["state"] == "running" else "server_live_session_interrupted"
            return {"status": "unverified", "reason": reason}
        try:
            finished = _number(result["finished_at"])
            if finished < _number(self.clock()) - self.retention:
                return {"status": "unverified", "reason": "server_live_observation_expired"}
            result["metadata"] = json.loads(result["metadata"])
            if not isinstance(result["metadata"], dict) or result["identity_source"] not in IDENTITY_SOURCES:
                raise ServerSessionError("invalid_server_observation")
            _build(result["metadata"].get("game_build"))
            _text(result["metadata"].get("map"), 512)
            _roster(result["metadata"].get("participants"))
        except (ValueError, TypeError, RecursionError):
            return {"status": "unverified", "reason": "server_live_observation_invalid"}
        return {"status": "finished", "observation": result}

    def finished_session(self, server_id, session_id, *, server_epoch=None):
        evidence = self.session_evidence(server_id, session_id, server_epoch=server_epoch)
        return evidence.get("observation") if evidence["status"] == "finished" else None


class ServerSessionIndex:
    """Associate a native session with a unique completed server-owned report.

    An archive completion is itself authoritative server evidence. Optional
    ledger confirmation is reported separately and can be required by a host.
    A unique exact server/session observation supplies its own server_epoch;
    a client need not know it. Related contradictory ledger state always
    remains pending, including when live observation is not mandatory.
    No client replay file, header hash, filename or timestamp is required.
    """
    def __init__(self, archive_path, *, ledger=None, require_live_observation=False):
        self.path = Path(archive_path).resolve()
        if not self.path.is_file():
            raise ServerSessionError("archive_database_missing")
        self.ledger, self.require_live_observation = ledger, require_live_observation

    def __call__(self, payload):
        if not isinstance(payload, dict):
            raise ServerSessionError("invalid_native_session_submission")
        session = _text(payload.get("session_id"))
        build = _build(payload.get("game_build"))
        roster = _roster(payload.get("native_players"), native=True)
        room = payload.get("room_hint")
        if room is not None:_server(room)
        map_id = payload.get("map")
        if map_id is not None:_text(map_id, 512)
        epoch = payload.get("server_epoch")
        if epoch is not None:_text(epoch)
        args = [session, session]
        room_filter = ""
        if room is not None:
            room_filter = " AND s.server_id=?";args.append(room)
        with closing(open_readonly_database(self.path)) as db:
            db.execute("PRAGMA query_only=ON")
            rows = db.execute("""SELECT DISTINCT r.sha256,r.payload,r.completion_status
                FROM reports r JOIN report_sources s ON s.sha256=r.sha256
                WHERE (r.match_key=? OR r.match_key='server:'||s.server_id||':'||?)"""
                + room_filter + " LIMIT 65", args).fetchall()
            if len(rows) > 64:
                return {"pending_reason": "server_session_archive_ambiguous"}
            matches = {}
            live_conflict = None
            for sha, encoded, completion in rows:
                if not isinstance(sha, str) or not _HASH.fullmatch(sha) or not isinstance(encoded, str) or len(encoded) > 64 * 1024 * 1024:
                    continue
                try:
                    report = json.loads(encoded)
                    source, match = report["source"], report["match"]
                    if (type(report.get("schema")) is not int or report["schema"] != 1
                            or source.get("origin") != "server_collection" or source.get("sha256") != sha
                            or completion != "finished" or match.get("completion", {}).get("status") != "finished"
                            or match.get("session_id") != session or match.get("version") != build
                            or type(match.get("version")) is not int
                            or map_id is not None and match.get("map") != map_id
                            or _roster(report.get("players")) != roster):
                        continue
                    archive_map = _text(match.get("map"), 512)
                    rooms = sorted({row[0] for row in db.execute("SELECT server_id FROM report_sources WHERE sha256=?", (sha,))
                                    if isinstance(row[0], str) and _ROOM.fullmatch(row[0])})
                    if not rooms or len(rooms) > 64 or room is not None and room not in rooms:
                        continue
                    verified = []
                    if self.ledger is not None:
                        for identifier in ([room] if room is not None else rooms):
                            evidence = self.ledger.session_evidence(identifier, session, server_epoch=epoch)
                            if evidence["status"] == "missing":
                                continue
                            if evidence["status"] != "finished":
                                live_conflict = evidence["reason"]
                                continue
                            observation = evidence["observation"]
                            if (observation["metadata"]["game_build"] != build
                                    or observation["metadata"]["map"] != archive_map
                                    or _roster(observation["metadata"]["participants"]) != roster):
                                live_conflict = "server_live_metadata_mismatch"
                                continue
                            verified.append(observation)
                    if len(verified) > 1:
                        live_conflict = "server_live_state_ambiguous"
                    if self.require_live_observation and not verified:
                        continue
                    matches[sha] = {"server_sha256": sha, "archive_rooms": rooms, "archive_map": archive_map,
                                    "binding_evidence": "server_finished_archive",
                                    "server_state_verified": bool(verified),
                                    "server_epoch": verified[0]["server_epoch"] if len(verified) == 1 else None,
                                    "server_id": verified[0]["server_id"] if len(verified) == 1 else None,
                                    "server_observations": [{"server_id": row["server_id"],
                                        "server_epoch": row["server_epoch"], "server_round_id": row["server_round_id"]}
                                        for row in verified]}
                except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                    continue
        if live_conflict is not None:
            return {"pending_reason": live_conflict}
        if len(matches) != 1:
            return {"pending_reason": "server_session_archive_ambiguous" if matches else
                    "server_live_state_unverified" if rows and self.require_live_observation else
                    "server_session_not_finished_or_unmatched"}
        return next(iter(matches.values()))
