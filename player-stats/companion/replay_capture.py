"""Automatic local replay report capture from observed game file I/O.

The observer supplies actual process file reads/writes, never a newest-file
guess. File hashing runs independently of native report sampling. Hash equality
binds the eventual submission to archive bytes; it does not authenticate a
client's report or prove that every file I/O request was a playback action.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import struct
import threading
import time

from . import __version__
from . import replay_evidence
from .replays import HeaderError, MAX_HEADER, ReplayIdentity, parse_header
from .service import (CompanionService, LOCAL_REPORT_SCHEMA, TASK_REPORT_SCHEMA, SCAN_STATES,
                      atomic_json, public_report_data, report_dict, report_quality, report_scan_progress)
from .state import report_key, roster_matches


MAX_REPLAY_BYTES = 1024 * 1024 * 1024
MAX_HEADER_PROBES = 2000
MAX_FILE_EVENTS = 256


class ReplayFileError(ValueError):
    def __init__(self, code, *, retryable=True):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _signature(stat):
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@dataclass(frozen=True)
class ReplayFileSnapshot:
    identity: ReplayIdentity
    sha256: str | None
    stream_offset: int
    size: int
    signature: tuple


def _header_prefix(source):
    data = source.read(16)
    if len(data) != 16 or data[:8] != b"ESAV\0\0\0\x03":
        raise ReplayFileError("unsupported_replay_header", retryable=False)
    for _ in range(16):
        block = source.read(16)
        if len(block) != 16:
            raise ReplayFileError("incomplete_replay_file")
        tag, size, reserved = struct.unpack(">4sQI", block)
        if reserved or size > MAX_HEADER or len(data) + 16 + size > MAX_HEADER + 32:
            raise ReplayFileError("invalid_replay_header", retryable=False)
        payload = source.read(size)
        if len(payload) != size:
            raise ReplayFileError("incomplete_replay_file")
        data += block + payload
        if tag == b"rply":
            return data
        if tag != b"modd":
            raise ReplayFileError("unsupported_replay_header_block", retryable=False)
    raise ReplayFileError("missing_replay_identity", retryable=False)


def _body_event(events, stream_offset, size, *, before=float("inf")):
    return next((event for event in events
                 if event[3] <= before and event[0] < size
                 and event[0] + event[1] > stream_offset), None)


def inspect_replay_file(path, *, observed_at=0.0, events=None,
                        max_bytes=MAX_REPLAY_BYTES, stop_event=None):
    """Read a bounded stable file, hashing its entire original byte sequence.

    With ``events``, header-only accesses return a probe without a file hash.
    This avoids hashing every file enumerated in the game's replay list.
    Event tuples are (byte_offset, read_size, operation, observed_at, sequence).
    """
    path = Path(path)
    if path.suffix.lower() not in (".rpl3", ".rpl"):
        raise ReplayFileError("unsupported_replay_extension", retryable=False)
    try:
        before = path.stat()
        if not 32 < before.st_size <= max_bytes:
            raise ReplayFileError("replay_file_size_out_of_range", retryable=before.st_size <= max_bytes)
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            prefix = _header_prefix(source)
            declared = struct.unpack_from(">I", prefix, 8)[0]
            if declared != before.st_size or opened.st_size != before.st_size or len(prefix) >= before.st_size:
                raise ReplayFileError("incomplete_replay_file")
            try:
                identity = parse_header(prefix, source_path=str(path), observed_at=observed_at)
            except HeaderError as exc:
                raise ReplayFileError("invalid_replay_header", retryable=False) from exc
            digest = None
            if events is None or _body_event(events, len(prefix), before.st_size) is not None:
                digest = hashlib.sha256()
                source.seek(0)
                total = 0
                while block := source.read(1024 * 1024):
                    if stop_event is not None and stop_event.is_set():
                        raise ReplayFileError("replay_hash_cancelled")
                    total += len(block)
                    if total > max_bytes or total > before.st_size:
                        raise ReplayFileError("replay_file_changed")
                    digest.update(block)
                if total != before.st_size:
                    raise ReplayFileError("replay_file_changed")
            after_open = os.fstat(source.fileno())
        after = path.stat()
        if (_signature(before) != _signature(after)
                or _signature(opened) != _signature(after_open)
                or opened.st_size != after.st_size):
            raise ReplayFileError("replay_file_changed")
        return ReplayFileSnapshot(identity, digest.hexdigest() if digest is not None else None,
                                  len(prefix), before.st_size, _signature(after))
    except ReplayFileError:
        raise
    except OSError as exc:
        raise ReplayFileError("replay_file_not_ready") from exc


@dataclass
class _Candidate:
    path: Path
    events: list = field(default_factory=list)
    version: int = 0
    future: object = None
    future_version: int = 0
    snapshot: ReplayFileSnapshot | None = None
    retry_at: float = 0.0
    terminal_error: str | None = None
    invalidated: bool = False
    observed_signature: tuple | None = None
    invalidated_before: float = -1.0


@dataclass
class _Process:
    baselined: bool = False
    baseline_key: tuple | None = None
    clear_seen: bool = False
    candidates: dict = field(default_factory=dict)
    handled: set = field(default_factory=set)
    pending_report: dict | None = None
    report_session: dict | None = None
    report_at: float | None = None
    latest_report: tuple | None = None
    blocked: bool = False
    opened_at: float = 0.0
    report_barrier_at: float = 0.0


class ReplayCaptureService:
    """Capture native reports after a uniquely corroborated local replay I/O."""
    flush = CompanionService.flush
    _emit = CompanionService._emit

    def __init__(self, config, observer, outbox, *, uploader=None, emit=None,
                 clock=time.time, executor=None):
        self.config, self.observer, self.outbox = config, observer, outbox
        self.uploader, self.emit, self.clock = uploader, emit or (lambda value: None), clock
        self.last_status = None
        self.processes = {}
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="sd2-replay-hash")
        self._owns_executor = executor is None
        self._stop = threading.Event()
        self._local_reports = set()
        self._local_metadata = {}
        self._pending_local = {}
        self._pending_exports = {}
        self._evidence_requested = set()
        self._evidence_failed = {}
        self.status = "waiting_for_replay_file_access"

    def _clear(self, state, now):
        # Loading may read B before the next poll observes A's result clear.
        # Preserve only actual I/O later than the previous report/baseline;
        # never recover pre-loss evidence from an uncertain access epoch.
        for key, candidate in list(state.candidates.items()):
            events = [] if state.blocked else [event for event in candidate.events
                                               if event[3] > state.report_barrier_at]
            if not events:
                if candidate.future is not None:
                    candidate.future.cancel()
                del state.candidates[key]
            else:
                candidate.events = events
                candidate.version += 1
        state.pending_report = None
        state.report_at = None
        state.blocked = False
        state.opened_at = now

    @staticmethod
    def _event_dict(event):
        if isinstance(event, dict):
            return event
        if is_dataclass(event):
            return asdict(event)
        return vars(event)

    def _event(self, state, process_key, raw, now):
        event = self._event_dict(raw)
        if event.get("process_key") != process_key:
            return
        offset = event.get("byte_offset", event.get("offset"))
        size = event.get("read_size", event.get("size"))
        sequence = event.get("sequence")
        operation = event.get("operation", "read")
        at = event.get("observed_at")
        if (type(offset) is not int or offset < 0 or type(size) is not int or not 0 < size <= MAX_REPLAY_BYTES
                or type(sequence) is not int or sequence < 0 or operation not in ("read", "write")
                or not isinstance(at, (int, float)) or isinstance(at, bool) or not 0 <= at <= now + 1
                or not isinstance(event.get("path"), (str, Path))):
            state.blocked = True
            self.status = "replay_access_evidence_invalid"
            return
        # Such a short request cannot have reached any ESAV command stream.
        if offset + size <= 32:
            return
        path = Path(event["path"])
        if not path.is_absolute() or path.suffix.lower() not in (".rpl3", ".rpl"):
            return
        state.opened_at = now
        key = os.path.normcase(str(path))
        if key not in state.candidates:
            # A replay list may probe hundreds of files. Bound these probes
            # without treating their count as playback ambiguity.
            if len(state.candidates) >= getattr(self.config, "max_replay_files", MAX_HEADER_PROBES):
                state.blocked = True
                self.status = "replay_header_probe_limit"
                return
            state.candidates[key] = _Candidate(path)
        candidate = state.candidates[key]
        if any(previous[4] == sequence for previous in candidate.events):
            return
        try:
            signature = _signature(path.stat())
        except OSError:
            signature = None
        if (candidate.observed_signature is not None and signature is not None
                and candidate.observed_signature != signature):
            # A path can be overwritten while an earlier hash is queued.
            # New bytes require their own I/O evidence and later report.
            if candidate.future is not None:
                candidate.future.cancel()
                candidate.future = None
            candidate.events.clear()
            candidate.snapshot = None
            candidate.invalidated_before = float(at)
            candidate.invalidated = False
            candidate.terminal_error = None
        candidate.observed_signature = signature
        if candidate.snapshot is not None:
            try:
                changed = _signature(path.stat()) != candidate.snapshot.signature
            except OSError:
                changed = True
            if changed:
                candidate.snapshot = None
                candidate.events.clear()
                candidate.invalidated = False
                candidate.terminal_error = None
        item = (offset, size, operation, float(at), sequence)
        after_report = state.report_at is not None and at > state.report_at
        # Coalesce contiguous I/O without inventing access to an unread gap.
        # The latest timestamp proves the combined interval had been accessed
        # by that time, while pre/post-report intervals remain separate.
        for index, previous in enumerate(candidate.events):
            previous_after = state.report_at is not None and previous[3] > state.report_at
            if (previous[2] == operation and previous_after == after_report
                    and max(previous[0], offset) <= min(previous[0] + previous[1], offset + size)):
                start = min(previous[0], offset)
                end = max(previous[0] + previous[1], offset + size)
                candidate.events[index] = (start, end - start, operation, max(previous[3], float(at)),
                                           max(previous[4], sequence))
                candidate.version += 1
                if candidate.snapshot and candidate.snapshot.sha256 is None:
                    candidate.snapshot = None
                return
        if candidate.snapshot and candidate.snapshot.sha256 is not None:
            # One proved body access is sufficient for this unchanged file.
            # Keep a separate post-report access for a subsequent playback.
            equivalent = [previous for previous in candidate.events
                          if previous[2] == operation
                          and (state.report_at is not None and previous[3] > state.report_at) == after_report]
            if _body_event(equivalent, candidate.snapshot.stream_offset, candidate.snapshot.size):
                return
        if len(candidate.events) >= MAX_FILE_EVENTS:
            state.blocked = True
            self.status = "replay_access_evidence_limit"
            return
        candidate.events.append(item)
        candidate.version += 1
        if candidate.invalidated:
            candidate.snapshot = None
            candidate.invalidated = False
            candidate.terminal_error = None
        # A new access can supply body evidence after a header-only probe.
        if candidate.snapshot and candidate.snapshot.sha256 is None:
            candidate.snapshot = None
        self.status = "replay_file_access_observed"

    def _hashes(self, state, now):
        for candidate in state.candidates.values():
            if candidate.future is not None and candidate.future.done():
                future, candidate.future = candidate.future, None
                try:
                    result = future.result()
                except ReplayFileError as exc:
                    self.status = exc.code
                    if exc.retryable:
                        candidate.retry_at = now + 1.0
                    else:
                        candidate.terminal_error = exc.code
                else:
                    if (result.sha256 is not None and candidate.observed_signature is not None
                            and result.signature != candidate.observed_signature
                            and not any(event[2] == "write" for event in candidate.events)):
                        candidate.invalidated = True
                        candidate.invalidated_before = now
                        candidate.events.clear()
                        candidate.snapshot = None
                        self.status = "replay_file_changed_after_read"
                        continue
                    candidate.snapshot = result
                    candidate.retry_at = 0
                    if result.sha256 is None and candidate.version != candidate.future_version:
                        candidate.snapshot = None
            if (candidate.future is None and candidate.snapshot is None and not candidate.invalidated
                    and candidate.terminal_error is None and now >= candidate.retry_at):
                candidate.future_version = candidate.version
                candidate.future = self._executor.submit(
                    inspect_replay_file, candidate.path, observed_at=min(e[3] for e in candidate.events),
                    events=tuple(candidate.events), stop_event=self._stop,
                    max_bytes=getattr(self.config, "max_replay_bytes", MAX_REPLAY_BYTES))
        hashed = {candidate.snapshot.sha256 for candidate in state.candidates.values()
                  if candidate.snapshot is not None and candidate.snapshot.sha256 is not None}
        if len(hashed) == 1 and all(candidate.snapshot is not None for candidate in state.candidates.values()):
            name = next(candidate.path.name for candidate in state.candidates.values()
                        if candidate.snapshot.sha256 is not None)
            self._emit("replay_file_identified", replay_sha256=next(iter(hashed)), replay_name=name)
            self._request_evidence(state, next(iter(hashed)))

    def _request_evidence(self, state, sha256):
        """Parse the replay once so the local analysis can estimate investment P.

        The server keeps production requests in its archive; a companion that
        only has the task report has no P. Parsing here is read only and runs off
        the UI thread; a failure only means this match stays without P.
        """
        if sha256 in self._evidence_requested or self._evidence_failed.get(sha256):
            return
        candidate = next((item for item in state.candidates.values()
                          if item.snapshot is not None and item.snapshot.sha256 == sha256), None)
        if candidate is None:
            return
        self._evidence_requested.add(sha256)
        path, directory = candidate.path, self.config.data_directory

        def work():
            try:
                replay_evidence.extract(path, sha256, directory)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, MemoryError, ImportError):
                # Retry once per process at most; never surface a path or traceback.
                # A build without the replay parser simply keeps matches without P.
                self._evidence_failed[sha256] = True
                self.status = "replay_evidence_unavailable"

        try:
            self._executor.submit(work)
        except RuntimeError:
            self._evidence_requested.discard(sha256)

    def _save_unbound(self, process_key, state):
        report = state.pending_report
        local_key = (process_key, *report_key(report))
        hint = self._identity_hint(state)
        self._pending_local[local_key] = (report, hint)
        self._write_local(local_key, report, hint)

    @staticmethod
    def _identity_hint(state):
        """Keep the report's verified replay context even without file I/O.

        ``report_session`` was copied when this new report was first observed,
        after the observer verified a stable replay context. Never borrow a
        later poll's session: the report may already belong to another replay.
        """
        report = getattr(state, "pending_report", None)
        session = getattr(state, "report_session", None)
        if (isinstance(report, dict) and isinstance(session, dict) and session.get("source_kind") == "replay"
                and session.get("is_replay") is True
                and session.get("game_build") == report.get("game_build")
                and roster_matches(report.get("players"), session.get("participants", ()))
                and isinstance(session.get("session_id"), str) and session["session_id"]
                and isinstance(session.get("map"), str) and session["map"]):
            return {**{key: session[key] for key in ("map", "server_name", "session_id")
                       if isinstance(session.get(key), str) and session[key]},
                    "session_origin": "native_replay_context"}
        identities = {}
        for candidate in state.candidates.values():
            snapshot = getattr(candidate, "snapshot", None)
            identity = getattr(snapshot, "identity", None) if snapshot is not None else None
            if identity is None:
                continue
            if isinstance(report, dict) and (identity.game_build != report.get("game_build")
                    or not roster_matches(report.get("players"), identity.participants)):
                continue
            identities[(getattr(identity, "map", None), getattr(identity, "server_name", None),
                         getattr(identity, "session_id", None))] = identity
        if len(identities) != 1:
            return {}
        identity = next(iter(identities.values()))
        hint = {"map": getattr(identity, "map", None), "server_name": getattr(identity, "server_name", None),
                "session_id": getattr(identity, "session_id", None)}
        return {key: value for key, value in hint.items() if isinstance(value, str) and value}

    def _write_local(self, local_key, report, identity_hint=None):
        hint = identity_hint or {}
        previous = self._local_metadata.get(local_key, {})
        if local_key in self._local_reports and all(previous.get(key) == value for key, value in hint.items()):
            self._pending_local.pop(local_key, None)
            return
        name = hashlib.sha256(json.dumps(local_key).encode()).hexdigest() + ".json"
        path = self.config.data_directory / "unbound-replays" / name
        public = public_report_data(report)
        first_write = local_key not in self._local_reports
        if first_write:
            value = {"schema": LOCAL_REPORT_SCHEMA, "report": public,
                     "status": "local_only_not_session_verified", **hint}
            atomic_json(path, value)
        else:
            # A header hash can finish after the first report snapshot. Add its
            # metadata to that same snapshot, keeping all native rows, other
            # local annotations, and the original capture time intact.
            before = path.stat()
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema") != LOCAL_REPORT_SCHEMA or value.get("report") != public:
                raise ValueError("local_replay_snapshot_changed")
            if any(value.get(key) and value[key] != item for key, item in hint.items()):
                # Conflicting identities cannot be combined into a new one.
                self._pending_local.pop(local_key, None)
                return
            value.update(hint)
            atomic_json(path, value)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self._local_reports.add(local_key)
        self._local_metadata[local_key] = {**previous, **hint}
        self._pending_local.pop(local_key, None)
        if first_write:
            self._emit("task_report_captured", rows=len(report["rows"]), **report_quality(report))

    @staticmethod
    def _commit(state, key, cutoff):
        state.handled.add(key)
        if state.pending_report is not None and report_key(state.pending_report) == key:
            state.pending_report = None
            state.report_at = None
        # Preserve accesses which occurred after this report for the next run.
        for name, candidate in list(state.candidates.items()):
            candidate.events = [event for event in candidate.events if event[3] > cutoff]
            if not candidate.events:
                if candidate.future is not None:
                    candidate.future.cancel()
                del state.candidates[name]

    def _write_export(self, payload):
        atomic_json(self.config.data_directory / "exports" / payload["client_replay_sha256"] /
                    (payload["submission_id"] + ".json"), payload)
        self.outbox.enqueue(payload)

    def _recover_pending(self):
        # Report replacement/clear cannot discard a snapshot whose first disk
        # or SQLite write failed. These buffers survive the native UI cycle.
        for local_key, (report, hint) in list(self._pending_local.items()):
            self._write_local(local_key, report, hint)
        for submission_id, (payload, name, state, key, cutoff) in list(self._pending_exports.items()):
            self._write_export(payload)
            self._commit(state, key, cutoff)
            del self._pending_exports[submission_id]
            self._emit("replay_report_queued", rows=len(payload["rows"]),
                       replay_sha256=payload["client_replay_sha256"], replay_name=name, **report_quality(payload))

    def _capture(self, process_key, state, now):
        report = state.pending_report
        if report is None:
            return
        self._save_unbound(process_key, state)
        if state.blocked:
            self.status = "replay_access_uncertain_report_retained"
            return
        bindings = {}
        unresolved = False
        for candidate in state.candidates.values():
            if candidate.invalidated_before >= state.report_at:
                # Losing one candidate's bytes cannot make another old
                # candidate become the uniquely selected replay by exclusion.
                unresolved = True
                continue
            before = [event for event in candidate.events if event[3] <= state.report_at]
            if not before:
                continue
            snapshot = candidate.snapshot
            if snapshot is None:
                unresolved = True
                continue
            evidence = _body_event(before, snapshot.stream_offset, snapshot.size)
            if evidence is None:
                continue
            if snapshot.sha256 is None:
                unresolved = True
                continue
            try:
                changed = _signature(candidate.path.stat()) != snapshot.signature
            except OSError:
                changed = True
            if changed:
                candidate.invalidated = True
                candidate.invalidated_before = now
                candidate.snapshot = None
                candidate.events.clear()
                self.status = "replay_file_changed_after_read"
                unresolved = True
                continue
            identity = snapshot.identity
            if (identity.game_build != report["game_build"]
                    or report.get("map") and identity.map != report["map"]
                    or not roster_matches(report.get("players"), identity.participants)):
                continue
            native_session = state.report_session
            if (native_session is not None and
                    (identity.session_id != native_session["session_id"]
                     or identity.game_build != native_session["game_build"]
                     or identity.map != native_session["map"])):
                continue
            bindings[snapshot.sha256] = (snapshot, evidence, candidate.path.name)
        if unresolved:
            self.status = "replay_report_waiting_for_stable_file"
            return
        if len(bindings) != 1:
            self.status = "replay_report_waiting_for_unique_file" if len(bindings) > 1 else "replay_report_waiting_for_matching_file"
            return
        sha, (snapshot, evidence, replay_name) = next(iter(bindings.items()))
        immutable = {"schema": TASK_REPORT_SCHEMA, "installation_id": self.outbox.installation_id, "process_key": process_key,
                     "report_key": report_key(report), "replay_sha256": sha}
        submission_id = hashlib.sha256(json.dumps(immutable, sort_keys=True).encode()).hexdigest()
        operation = evidence[2]
        payload = {"schema": TASK_REPORT_SCHEMA, "submission_id": submission_id,
                   "installation_id": self.outbox.installation_id, "session_id": snapshot.identity.session_id,
                   "map": snapshot.identity.map, "server_name": snapshot.identity.server_name,
                   **public_report_data(report),
                   "extractor_version": __version__, "source": "player_client_official_task_report",
                   "binding_mode": "replay_sha256", "client_replay_sha256": sha,
                   "client_context_evidence": {"association": "observed_game_file_" + operation,
                                               "file_operation": operation,
                                               "roster_check": "exact_player_user_id_alliance_multiset",
                                               "file_body_io_observed": True}}
        from .receiver import validate_payload
        validate_payload(payload)
        self._pending_exports[submission_id] = (payload, replay_name, state, report_key(report), state.report_at)
        self._write_export(payload)
        self._commit(state, report_key(report), state.report_at)
        del self._pending_exports[submission_id]
        self.status = "replay_report_queued"
        self._emit(self.status, rows=len(payload["rows"]), replay_sha256=sha, replay_name=replay_name,
                   **report_quality(payload))

    def tick(self):
        now = self.clock()
        observation = self.observer.poll(now)
        process_key = observation.get("process_key")
        if process_key:
            state = self.processes.setdefault(process_key, _Process(opened_at=now))
            clear = observation.get("report_identity_clear", False)
            if clear and not state.clear_seen:
                self._clear(state, now)
            state.clear_seen = bool(clear)
            if observation.get("baseline_ready") and not state.baselined:
                baseline = observation.get("baseline_report")
                if baseline is not None:
                    baseline = report_dict(baseline)
                    state.baseline_key = report_key(baseline)
                    state.handled.add(state.baseline_key)
                state.baselined = True
                state.report_barrier_at = now
            if observation.get("access_uncertain"):
                state.blocked = True
            for event in observation.get("file_events", ()):
                self._event(state, process_key, event, now)
            self._hashes(state, now)
            raw = observation.get("report")
            if raw is not None:
                report = report_dict(raw)
                key = report_key(report)
                if not state.baselined:
                    state.baseline_key = key
                    state.handled.add(key)
                    state.baselined = True
                    state.report_barrier_at = now
                elif key not in state.handled and key != state.latest_report:
                    if state.pending_report is not None:
                        self._save_unbound(process_key, state)
                    state.pending_report = report
                    session = observation.get("native_session")
                    state.report_session = (dict(session) if isinstance(session, dict)
                        and session.get("source_kind") == "replay" and session.get("is_replay") is True
                        and observation.get("report_context_stable") is True else None)
                    state.report_at = now
                    state.report_barrier_at = now
                    state.opened_at = now
                    self._pending_local[(process_key, *key)] = (report, self._identity_hint(state))
                state.latest_report = key
            self._capture(process_key, state, now)
        # Cached reports can finish hashing after their original process exits.
        for key, state in list(self.processes.items()):
            if key != process_key:
                self._hashes(state, now)
                self._capture(key, state, now)
            if now - state.opened_at > self.config.report_timeout_seconds:
                for candidate in state.candidates.values():
                    if candidate.future is not None:
                        candidate.future.cancel()
                del self.processes[key]
        self._recover_pending()
        native_status = observation.get("native_status")
        status = observation.get("error") or (native_status if native_status in SCAN_STATES else self.status)
        self._emit(status, **self.outbox.status(), **report_scan_progress(observation))
        return observation

    def close(self):
        self._stop.set()
        close = getattr(self.observer, "close", None)
        if close:
            close()
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
