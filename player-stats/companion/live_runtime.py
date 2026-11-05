"""Snapshot a native session around report reads; file monitoring is optional."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass

from .native_report import NativeReportError
from .native_stats import NativeStatsReader
from .replay_runtime import ReplayWindowsObserver


class _NoFileMonitor:
    status = "file_monitor_running"
    def start(self):pass
    def set_process(self, pid, key):pass
    def poll(self):return []
    def close(self):pass


class AutomaticWindowsObserver(ReplayWindowsObserver):
    def __init__(self, config, *, monitor=None, with_replays=True, catalog=None):
        if not with_replays:
            monitor = _NoFileMonitor()
        super().__init__(config, monitor, monitor_required=False, catalog=catalog)
        self.session_reader = None
        self.session_reader_key = None
        self.stats_reader = None

    def _reset(self):
        if self.stats_reader is not None:
            self.stats_reader.reset()
        self.stats_reader = None
        self.session_reader = None
        self.session_reader_key = None
        super()._reset()

    def _session(self, result):
        try:
            value = self.session_reader.poll(process_key=self.key)
            if value is None:
                result["native_session_status"] = self.session_reader.status
                return None
            result["native_session_status"] = self.session_reader.status
            if hasattr(value, "to_dict"):
                return value.to_dict()
            return asdict(value) if is_dataclass(value) else value
        except NativeReportError as exc:
            result["native_session_status"] = exc.code
            return None

    def _poll_native(self, result):
        if self.session_reader_key != self.key:
            from .native_session import NativeSessionReader
            self.session_reader = NativeSessionReader(self.memory)
            self.stats_reader = NativeStatsReader(self.memory)
            self.session_reader_key = self.key
        before = self._session(result)
        self._poll_stats(result, before)
        native = self.reader.poll()
        after = self._session(result)
        result["native_session"] = after
        # Ended may progress false->true during the report read. Identity and
        # mode must stay exactly the same across that read, including roster.
        keys = ("session_id", "context_epoch", "source_kind", "is_replay", "map", "game_build", "participants")
        result["report_context_stable"] = bool(before and after and
            all(before.get(key) == after.get(key) for key in keys) and
            not (before.get("game_ended") is True and after.get("game_ended") is False))
        if before and after and before.get("context_epoch") != after.get("context_epoch"):
            result["native_context_changed"] = True
        if not result["report_context_stable"]:
            self.stats_reader.reset()
        elif native is not None:
            # A final death can be appended between the pre-report snapshot and
            # publication. Take the last stable history while it is available;
            # failed reads retain only this epoch's earlier validated material.
            self._poll_stats(result, after)
            try:
                native = self.stats_reader.bind_timeline(native, after)
            except NativeReportError as exc:
                result["native_stats_status"] = exc.code
        return native

    def _poll_stats(self, result, session):
        try:
            self.stats_reader.poll(session, process_key=self.key)
            result["native_stats_status"] = self.stats_reader.status
        except NativeReportError as exc:
            # Identity enrichment must never discard the official report.
            result["native_stats_status"] = exc.code
