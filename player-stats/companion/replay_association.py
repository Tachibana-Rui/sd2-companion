"""Explicitly submit a local report for matching against a server replay.

Local valuation links remain local. Only a player's explicit request enters the
delivery queue, and the server must still verify the complete replay digest
against its archive. No replay bytes or local paths are sent.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from . import __version__
from .local_replay_library import report_fingerprint
from .receiver import validate_payload
from .replay_capture import inspect_replay_file, _signature
from .service import TASK_REPORT_SCHEMA, atomic_json
from .state import roster_matches


_REPORT_FIELDS = ("game_build", "report_generation", "native_players", "rows", "locale",
                  "report_data", "exe_sha256")


def prepare_submission(report, replay_path, installation_id, *, session_id=None,
                       user_selected=False):
    """Build an honest manual-association payload without writing or uploading.

    A filename, nickname, duration, or newest-file guess cannot establish the
    relationship. Require the complete ID/side roster and build, additionally
    checking any session captured with this report.
    """
    if user_selected is not True:
        raise ValueError("server_replay_association_requires_selection")
    snapshot = inspect_replay_file(replay_path)
    identity = snapshot.identity
    if (identity.game_build != report.get("game_build")
            or not roster_matches(report.get("native_players"), identity.participants)):
        raise ValueError("server_replay_roster_or_build_mismatch")
    if session_id and session_id != identity.session_id:
        raise ValueError("server_replay_session_mismatch")
    # public_report_data has already removed process addresses. Copy only that
    # projection, rather than forwarding arbitrary local annotations.
    public = deepcopy({key: report[key] for key in _REPORT_FIELDS if key in report})
    material = {"schema": TASK_REPORT_SCHEMA, "installation_id": installation_id,
                "report_fingerprint": report_fingerprint(public),
                "client_replay_sha256": snapshot.sha256,
                "association": "user_selected_local_replay"}
    submission_id = hashlib.sha256(json.dumps(material, sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    payload = {"schema": TASK_REPORT_SCHEMA, "submission_id": submission_id,
               "installation_id": installation_id, "session_id": identity.session_id,
               "map": identity.map, "server_name": identity.server_name, **public,
               "extractor_version": __version__,
               "source": "player_client_official_task_report",
               "binding_mode": "replay_sha256", "client_replay_sha256": snapshot.sha256,
               "client_context_evidence": {
                   "association": "user_selected_local_replay",
                   "roster_check": "exact_player_user_id_alliance_multiset",
                   "full_replay_sha256_verified": True}}
    validate_payload(payload)
    if _signature(Path(replay_path).stat()) != snapshot.signature:
        raise ValueError("server_replay_changed")
    return payload


def _same_capture(previous, payload):
    """Reuse a durable export, including exports produced by automatic I/O."""
    keys = (*_REPORT_FIELDS, "installation_id", "session_id", "map",
            "client_replay_sha256", "binding_mode")
    return all(previous.get(key) == payload.get(key) for key in keys)


def enqueue_association(directory, report, replay_path, outbox, *, session_id=None,
                        user_selected=False):
    """Durably queue an explicit request; keep source reports and links intact.

    Reuse an identical existing capture, retaining its original extractor
    version and evidence. Repeated clicks, restarts, and upgrades therefore
    cannot alter an immutable submission or create another copy of it.
    """
    payload = prepare_submission(report, replay_path, outbox.installation_id,
                                 session_id=session_id, user_selected=user_selected)
    target = Path(directory) / "exports" / payload["client_replay_sha256"]
    for path in sorted(target.glob("*.json")):
        try:
            if path.stat().st_size > 64 * 1024 * 1024:
                continue
            previous = json.loads(path.read_text(encoding="utf-8"))
            validate_payload(previous)
        except (OSError, ValueError, TypeError, KeyError):
            continue
        if _same_capture(previous, payload):
            outbox.enqueue(previous)
            return previous
    path = target / (payload["submission_id"] + ".json")
    if path.exists():
        # Never overwrite an immutable export with different contents, even
        # when a damaged local file has the expected deterministic filename.
        raise ValueError("server_replay_export_content_conflict")
    atomic_json(path, payload)
    outbox.enqueue(payload)
    return payload
