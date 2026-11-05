"""Read-only game observer for automatic local replay capture, without TCP."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass

from .native_report import NativeReportError


class ReplayWindowsObserver:
    def __init__(self, config, monitor=None, *, monitor_required=True, catalog=None):
        from . import windows
        from .native_report import NativeReportReader
        if monitor is None:
            from .file_access import FileAccessMonitor
            monitor = FileAccessMonitor()
        self.windows, self.reader_type, self.config = windows, NativeReportReader, config
        # Remote tunables and the server-issued room table; memory only.
        self.catalog = catalog
        self.monitor = monitor
        self.monitor_required = monitor_required
        self.monitor_disabled = False
        self.monitor_error = None
        self.started = self.closed = False
        self.process = self.memory = self.reader = None
        self.key = None
        self.next_process_poll = 0
        self.baseline_ready = False
        self.baseline_report = None

    def _poll_native(self, result):
        return self.reader.poll()

    def _reset(self):
        self.monitor.set_process(None, None)
        if self.memory is not None:
            self.memory.close()
        self.process = self.memory = self.reader = None
        self.key = None
        self.baseline_ready = False
        self.baseline_report = None

    def poll(self, now):
        if self.closed:
            return {"error": "observer_closed"}
        if self.catalog is not None:
            # Bounded fetch when due, then apply only the pushed tunables.
            from .catalog import sync, with_tunables
            sync(self.catalog, now)
            self.config = with_tunables(self.config, self.catalog.settings)
        if not self.started and not self.monitor_disabled:
            if self.monitor.status not in ("file_monitor_not_started", "file_monitor_running"):
                self.monitor_error = self.monitor.status
                self.monitor_disabled = True
            else:
                try:
                    self.monitor.start()
                    self.started = True
                except (OSError, ValueError) as exc:
                    self.monitor_error = getattr(exc, "code", "file_monitor_start_failed")
                    self.monitor_disabled = True
        if self.monitor_disabled and self.monitor_required:
            return {"error": self.monitor_error, "access_uncertain": True}
        if now >= self.next_process_poll:
            self.next_process_poll = now + self.config.process_poll_seconds
            try:
                processes = self.windows.list_game_processes(self.config.game_executable)
            except OSError:
                return {"error": "process_discovery_failed"}
            if len(processes) != 1:
                self._reset()
                self.monitor.poll()  # Drain stale events after process exit.
                return {"error": "game_not_running" if not processes else "multiple_game_processes"}
            process = processes[0]
            process_key = f"{process.pid}:{process.creation_time_100ns}"
            if process_key != self.key:
                self._reset()
                try:
                    self.process = process
                    self.memory = self.windows.WindowsMemory(process)
                    self.reader = self.reader_type(self.memory)
                    self.key = process_key
                    self.monitor.set_process(process.pid, process_key)
                except NativeReportError as exc:
                    self._reset()
                    return {"error": exc.code}
                except (OSError, ValueError):
                    self._reset()
                    return {"error": "unsupported_game_or_process_unreadable"}
        if self.process is None:
            return {"error": "game_not_running"}
        result = {"process_key": self.key, "file_events": []}
        try:
            events = self.monitor.poll() if self.started else []
            result["file_events"] = [asdict(e) if is_dataclass(e) else e for e in events]
            status = self.monitor_error or self.monitor.status
            if status != "file_monitor_running":
                result["file_monitor_error"] = status
                if self.monitor_required:
                    result["error"] = status
                result["access_uncertain"] = True
            self.memory.reset_budget()
            native = self._poll_native(result)
            if not self.baseline_ready and self.reader.baseline_ready:
                self.baseline_report = native
                self.baseline_ready = True
            elif self.baseline_ready and native is not None:
                result["report"] = native
            result.update(baseline_ready=self.baseline_ready, baseline_report=self.baseline_report,
                          native_status=self.reader.status,
                          report_identity_clear=(self.reader.baseline_ready and
                                                 self.reader.status == "waiting_for_published_report"))
        except NativeReportError as exc:
            result["error"] = exc.code
        except (OSError, ValueError) as exc:
            result["error"] = getattr(exc, "code", "native_report_not_ready")
        result["native_status"] = getattr(self.reader, "status", "")
        diagnostic = getattr(self.reader, "diagnostics", None)
        if diagnostic is not None:
            result["native_diagnostics"] = diagnostic()
        return result

    def close(self):
        if not self.closed:
            try:
                self._reset()
            finally:
                self.monitor.close()
                self.closed = True
