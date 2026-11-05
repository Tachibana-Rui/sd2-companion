"""Collect live debriefs from native session lifecycle, without replay files."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import time

from . import __version__
from .service import (CompanionService, LOCAL_REPORT_SCHEMA, TASK_REPORT_SCHEMA, SCAN_STATES,
                      atomic_json, public_report_data, report_dict, report_quality, report_scan_progress)
from .state import report_key, roster_matches


@dataclass
class _LiveProcess:
    baselined: bool = False
    session: dict | None = None
    observed_running: bool = False
    ended_at: float | None = None
    handled: set = field(default_factory=set)
    consumed_epochs: set = field(default_factory=set)


class LiveCaptureService:
    """A native session and a new ended-report must be observed together.

    The server is the final authority on whether that exact session belongs to
    a managed, finished match. No file, header hash, nearest time, or nickname
    supplies a missing native session. A report first seen with unknown source
    stays local instead of acquiring a future session's identity.
    """
    flush = CompanionService.flush
    _emit = CompanionService._emit

    def __init__(self, config, observer, outbox, *, uploader=None, emit=None, clock=time.time):
        self.config, self.observer, self.outbox = config, observer, outbox
        self.uploader, self.emit, self.clock = uploader, emit or (lambda value: None), clock
        self.last_status = None
        self.processes = {}
        self.pending = {}
        self.status = "live_report_waiting_for_session"

    def _identity(self, state, session, now):
        if not isinstance(session, dict):
            return
        if session.get("source_kind") != "network_live" or session.get("is_replay") is not False:
            state.session = None
            state.observed_running = False
            state.ended_at = None
            return
        epoch = session.get("context_epoch")
        if not state.session or state.session.get("context_epoch") != epoch:
            state.session = dict(session)
            state.observed_running = False
            state.ended_at = None
            self.status = "live_session_observed"
            self._emit("live_session_observed", session_id=session["session_id"])
        if session.get("game_ended") is False:
            # An ended context reverting to running cannot reuse an old
            # eligibility window without a newly verified native epoch.
            if state.ended_at is not None:
                state.consumed_epochs.add(epoch)
            state.observed_running = True
        elif session.get("game_ended") is True and state.observed_running and state.ended_at is None:
            state.ended_at = now
        state.session = dict(session)

    def _payload(self, process_key, state, report, observation):
        session = observation.get("native_session")
        if (not isinstance(session, dict) or not observation.get("report_context_stable")
                or session.get("source_kind") != "network_live" or session.get("is_replay") is not False):
            self.status = "live_report_source_unknown"
            return None
        if (not state.session or state.session.get("context_epoch") != session.get("context_epoch")
                or not state.observed_running or state.ended_at is None or session.get("game_ended") is not True
                or session["context_epoch"] in state.consumed_epochs):
            self.status = "live_report_waiting_for_session"
            return None
        if (report["game_build"] != session.get("game_build")
                or not roster_matches(report.get("players"), session.get("participants", ()))):
            self.status = "live_report_wrong_session"
            return None
        immutable = {"schema": TASK_REPORT_SCHEMA, "installation_id": self.outbox.installation_id,
                     "process_key": process_key, "report_key": report_key(report),
                     "session_id": session["session_id"], "context_epoch": session["context_epoch"]}
        submission = hashlib.sha256(json.dumps(immutable, sort_keys=True).encode()).hexdigest()
        payload = {"schema": TASK_REPORT_SCHEMA, "submission_id": submission,
                   "installation_id": self.outbox.installation_id, "session_id": session["session_id"],
                   **public_report_data(report),
                   "extractor_version": __version__, "source": "player_client_official_task_report",
                   "binding_mode": "native_session",
                   "client_context_evidence": {"association": "native_game_session",
                       "session_origin": "native_game_setup", "native_source_kind": "network_live",
                       "native_game_ended": True, "observed_running": True,
                       "report_observed_after_end": True, "context_epoch": session["context_epoch"]}}
        if session.get("map"):
            payload["map"] = session["map"]
        if session.get("server_name"):
            payload["server_name"] = session["server_name"]
        from .receiver import validate_payload
        validate_payload(payload)
        return payload

    def _persist(self):
        for local_key, item in list(self.pending.items()):
            report, payload, state, epoch = item
            name = hashlib.sha256(json.dumps(local_key).encode()).hexdigest()
            # The local snapshot keeps the session map/server when the live session
            # already knows them, so the results list never shows a blank map for a
            # match that only exists locally.
            session = state.session if isinstance(state.session, dict) else {}
            metadata = {key: session.get(key) or (payload or {}).get(key)
                        for key in ("map", "server_name", "session_id")}
            atomic_json(self.config.data_directory / "unbound-live" / (name + ".json"),
                        {"schema": LOCAL_REPORT_SCHEMA, "report": public_report_data(report),
                         "status": "local_only_not_session_verified",
                         **{key: value for key, value in metadata.items() if isinstance(value, str) and value}})
            self._emit("task_report_captured", rows=len(report["rows"]), **report_quality(report))
            if payload is not None:
                session_hash = hashlib.sha256(payload["session_id"].encode()).hexdigest()
                atomic_json(self.config.data_directory / "exports" / "live" / session_hash /
                            (payload["submission_id"] + ".json"), payload)
                self.outbox.enqueue(payload)
                state.consumed_epochs.add(epoch)
                self.status = "live_report_queued"
                self._emit(self.status, session_id=payload["session_id"], rows=len(payload["rows"]),
                           **report_quality(payload))
            state.handled.add(report_key(report))
            del self.pending[local_key]

    def tick(self):
        now = self.clock()
        observation = self.observer.poll(now)
        key = observation.get("process_key")
        if key:
            state = self.processes.setdefault(key, _LiveProcess())
            if observation.get("baseline_ready") and not state.baselined:
                baseline = observation.get("baseline_report")
                if baseline is not None:
                    state.handled.add(report_key(report_dict(baseline)))
                state.baselined = True
            self._identity(state, observation.get("native_session"), now)
            raw = observation.get("report")
            if raw is not None:
                report = report_dict(raw)
                identity = report_key(report)
                local_key = (key, *identity)
                if not state.baselined:
                    state.baselined = True
                    state.handled.add(identity)
                elif identity not in state.handled and local_key not in self.pending:
                    payload = self._payload(key, state, report, observation)
                    epoch = state.session.get("context_epoch") if state.session else None
                    self.pending[local_key] = (report, payload, state, epoch)
        self._persist()
        native_status = observation.get("native_status")
        status = observation.get("error") or (native_status if native_status in SCAN_STATES else self.status)
        self._emit(status, **self.outbox.status(), **report_scan_progress(observation))
        return observation

    def close(self):
        close = getattr(self.observer, "close", None)
        if close:
            close()


class _BufferedObserver:
    def __init__(self):self.observation = {}
    def poll(self, now):return self.observation


class AutomaticCaptureService:
    """One native read, two separate identity policies and one upload queue."""
    flush = CompanionService.flush
    _emit = CompanionService._emit

    def __init__(self, config, observer, outbox, *, uploader=None, emit=None, clock=time.time):
        from .replay_capture import ReplayCaptureService
        self.config, self.observer, self.outbox = config, observer, outbox
        self.uploader, self.emit, self.clock = uploader, emit or (lambda value: None), clock
        self.last_status = None
        self.live_observer, self.replay_observer = _BufferedObserver(), _BufferedObserver()
        self.live = LiveCaptureService(config, self.live_observer, outbox, emit=self.emit, clock=clock)
        self.replay = ReplayCaptureService(config, self.replay_observer, outbox, emit=self.emit, clock=clock)
        self.last_monitor_error = None
        self.source_epochs = {}

    def tick(self):
        observation = self.observer.poll(self.clock())
        session = observation.get("native_session") or {}
        process_key = observation.get("process_key")
        source_epoch = (session.get("source_kind"), session.get("context_epoch"))
        previous_source = self.source_epochs.get(process_key)
        if process_key and source_epoch[0] in ("network_live", "replay"):
            if previous_source != source_epoch:
                # A recording written while playing live cannot later serve
                # as evidence that this process opened that file for replay.
                previous = self.replay.processes.get(process_key)
                if previous is not None:
                    for candidate in previous.candidates.values():
                        if candidate.future is not None:
                            candidate.future.cancel()
                    previous.candidates.clear()
                    self.replay._clear(previous, self.clock())
            self.source_epochs[process_key] = source_epoch
        replay = (session.get("source_kind") == "replay" and session.get("is_replay") is True
                  and observation.get("report_context_stable") is True)
        live = {**observation, "report": None if replay else observation.get("report")}
        replay_observation = {**observation, "report": observation.get("report") if replay else None}
        if source_epoch[0] == "network_live" or (not session and previous_source and previous_source[0] == "network_live"):
            replay_observation["file_events"] = []
        monitor_error = observation.get("file_monitor_error")
        if monitor_error:
            replay_observation["error"] = monitor_error
        if monitor_error and monitor_error != self.last_monitor_error:
            self.emit({"event": monitor_error})
            self.last_monitor_error = monitor_error
        self.live_observer.observation = live
        self.replay_observer.observation = replay_observation
        if replay:
            # Still drain durable live records already captured before a mode
            # switch, while the current report goes only through SHA binding.
            self.live.tick()
            self.replay.tick()
        else:
            self.replay.tick()
            self.live.tick()
        return observation

    def close(self):
        try:
            self.live.close()
            self.replay.close()
        finally:
            self.observer.close()
