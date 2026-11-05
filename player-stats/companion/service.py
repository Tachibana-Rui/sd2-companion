"""Coordination with injected platform observers; no OS side effects on import."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time

from .upload import UploadError


SCAN_STATES = frozenset(("locating_report_controller", "waiting_for_report_controller"))
TASK_REPORT_SCHEMA = "sd2-task-report-v4"
LOCAL_REPORT_SCHEMA = "sd2-local-unbound-task-report-v4"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_GUID = re.compile(r"[0-9a-f]{32}\Z")


def public_report_data(report):
    """Project a captured native report without process addresses or arbitrary metadata.

    Rows and player records are passed through in native order, including every
    repeated event. NativeReport's pointer-based key remains internal only.
    """
    metadata = report.get("metadata", {})
    evidence = metadata.get("report_data")
    if not isinstance(evidence, dict):
        evidence = {"identity_source": "unavailable", "identity_status": "unavailable",
                    "matched_rows": 0, "total_rows": len(report["rows"])}
    if (evidence.get("identity_source") not in ("native_stats_history", "unavailable")
            or evidence.get("identity_status") not in ("complete", "partial", "unavailable", "ambiguous")
            or any(type(evidence.get(key)) is not int or not 0 <= evidence[key] <= len(report["rows"])
                   for key in ("matched_rows", "total_rows"))
            or evidence["total_rows"] != len(report["rows"])
            or evidence["matched_rows"] > evidence["total_rows"]):
        raise ValueError("invalid_report_data")
    value = {"game_build": report["game_build"], "report_generation": report["generation"],
             "native_players": report.get("players", []), "rows": report["rows"],
             "locale": report.get("locale", "unknown"),
             "report_data": {key: evidence[key] for key in
                 ("identity_source", "identity_status", "matched_rows", "total_rows")}}
    fingerprint = metadata.get("exe_sha256")
    if not isinstance(fingerprint, str) or not _HASH.fullmatch(fingerprint):
        raise ValueError("invalid_report_executable_hash")
    value["exe_sha256"] = fingerprint
    return value


def report_quality(report):
    """Small coverage counters for the UI; never prices, names, or memory addresses."""
    players = report.get("native_players", report.get("players", []))
    actors = [row[role] for row in report["rows"] for role in ("killer", "victim")]
    scored = sum(all(type(player.get(key)) is int and 0 <= player[key] <= 0xffffffff
                     for key in ("official_kill_points", "official_loss_points")) for player in players)
    resolved = sum(actor.get("identity_status") == "resolved_native_stats"
                   and isinstance(actor.get("unit_guid"), str) and bool(_GUID.fullmatch(actor["unit_guid"]))
                   for actor in actors)
    return {"official_score_players": scored, "total_players": len(players),
            "resolved_actors": resolved, "total_actors": len(actors)}


def report_scan_progress(observation):
    """Small diagnostic counters; no runtime addresses or report contents."""
    if observation.get("native_status") not in SCAN_STATES:
        return {}
    diagnostic = observation.get("native_diagnostics")
    if not isinstance(diagnostic, dict):
        return {}
    scanned = diagnostic.get("scan_bytes_in_round", 0)
    if observation["native_status"] == "waiting_for_report_controller":
        scanned = diagnostic.get("scan_bytes_last_round", scanned)
    total = diagnostic.get("scan_bytes_planned", 0)
    rounds = diagnostic.get("scan_rounds_completed", 0)
    if any(type(value) is not int or value < 0 for value in (scanned, total, rounds)):
        return {}
    # Coarse progress prevents one console line for every memory block.
    return {"scan_mib": scanned // (128 * 1048576) * 128,
            "scan_total_mib": total // 1048576,
            "scan_rounds_completed": rounds}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".companion-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def report_dict(value):
    if isinstance(value, dict):
        result = value
    elif hasattr(value, "to_dict"):
        result = value.to_dict()
    elif is_dataclass(value):
        result = asdict(value)
    else:
        raise ValueError("unsupported_native_report")
    if "game_build" not in result and isinstance(result.get("metadata"), dict):
        result = {**result, "game_build": result["metadata"].get("game_build")}
    if not isinstance(result, dict) or not isinstance(result.get("rows"), list):
        raise ValueError("invalid_native_report")
    for required in ("variant_address", "generation", "result_address", "game_build"):
        if type(result.get(required)) is not int or result[required] < 0:
            raise ValueError("invalid_native_report_identity")
    return result


class CompanionService:
    """Shared durable delivery operations for the two automatic capture policies."""
    def __init__(self, outbox, *, uploader=None, emit=None):
        self.outbox, self.uploader = outbox, uploader
        self.emit = emit or (lambda value: None)
        self.last_status = None

    def _emit(self, status, **counts):
        value = {"event": status, **counts}
        if value != self.last_status:
            self.emit(value)
            self.last_status = value

    def flush(self, *, now=None, limit=2):
        if self.uploader is None:
            return 0
        count = 0
        for job in self.outbox.due(now=now, limit=limit):
            try:
                receipt = self.uploader.send(job["payload"])
            except UploadError as exc:
                if exc.retryable:
                    self.outbox.mark_retry(job["id"], exc.code, now=now, retry_after=exc.retry_after)
                else:
                    self.outbox.mark_rejected(job["id"], exc.code)
                self._emit("upload_retry" if exc.retryable else "upload_rejected", **self.outbox.status())
            else:
                self.outbox.mark_sent(job["id"], receipt)
                count += 1
                self._emit("upload_acknowledged", receipt_status=receipt["status"], **self.outbox.status())
        return count

class DeliveryWorker:
    """Network I/O never runs on the memory-sampling thread."""
    def __init__(self, service, *, interval=0.5):
        self.service = service
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread is not None:
            raise ValueError("delivery_worker_already_started")
        self.thread = threading.Thread(target=self._run, name="sd2-companion-upload", daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.service.flush(limit=1)
            except (OSError, ValueError, sqlite3.Error):
                # The row remains durable; retry rather than killing capture.
                self.service.emit({"event": "delivery_worker_retry"})
            self.stop_event.wait(self.interval)

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=1)

