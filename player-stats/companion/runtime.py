"""Windows observer used only when the user explicitly runs the companion."""
from __future__ import annotations

import ipaddress
import socket
from .native_report import NativeReportError


def canonical_address(value):
    address = ipaddress.ip_address(value.split("%", 1)[0])
    return str(address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address)


class WindowsObserver:
    def __init__(self, config, catalog=None):
        from . import windows
        from .native_report import NativeReportReader
        self.windows, self.reader_type, self.config = windows, NativeReportReader, config
        # The server-issued table wins over the local seed; it lives in memory.
        self.catalog = catalog
        self.process = self.memory = self.reader = None
        self.next_process_poll = 0
        self.key = None
        self.baseline_ready = False
        self.baseline_report = None
        self.endpoints = {}
        self.resolve_error = None
        self.next_dns_refresh = 0
        self.closed = False

    def rooms(self):
        """A configured catalog owns the table, including its empty state."""
        if self.catalog is not None:
            return self.catalog.rooms
        return self.config.rooms

    def _apply_settings(self, settings):
        from .catalog import with_tunables
        self.config = with_tunables(self.config, settings)

    def _resolve(self, now):
        if now < self.next_dns_refresh:
            return
        rooms = self.rooms()
        resolved, failed, stale = {}, False, self.endpoints
        for room in rooms:
            for endpoint in room.endpoints:
                try:
                    addresses = socket.getaddrinfo(endpoint.host, endpoint.port, type=socket.SOCK_STREAM)
                except OSError:
                    # A name that stops resolving must not stop capture: keep the
                    # pairs this room had before and retry on the next pass.
                    failed = True
                    for pair, owner in stale.items():
                        if owner.id == room.id and pair[1] == endpoint.port:
                            resolved[pair] = owner
                    continue
                for item in addresses:
                    pair = (canonical_address(item[4][0]), endpoint.port)
                    if pair in resolved and resolved[pair].id != room.id:
                        raise ValueError("resolved_endpoint_assigned_to_multiple_rooms")
                    resolved[pair] = room
        if resolved or not rooms:
            self.endpoints = resolved
        self.resolve_error = "target_endpoint_resolution_failed" if failed else None
        self.next_dns_refresh = now + (10 if failed else 60)

    def _reset(self):
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
            # One bounded fetch per interval, inside the poll loop: no thread,
            # and a failure keeps the previous table and address pairs.
            if self.catalog.refresh_if_due(now):
                self.next_dns_refresh = 0
            self._apply_settings(self.catalog.settings)
        try:
            self._resolve(now)
        except ValueError:
            return {"error": "target_endpoint_resolution_failed"}
        if now >= self.next_process_poll:
            self.next_process_poll = now + self.config.process_poll_seconds
            try:
                processes = self.windows.list_game_processes(self.config.game_executable)
            except OSError:
                return {"error": "process_discovery_failed"}
            if len(processes) != 1:
                self._reset()
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
                except NativeReportError as exc:
                    self._reset()
                    return {"error": exc.code}
                except (OSError, ValueError):
                    self._reset()
                    return {"error": "unsupported_game_or_process_unreadable"}
        if self.process is None:
            return {"error": "game_not_running"}
        result = {"process_key": self.key, "room_id": None, "connection_observed": False}
        try:
            connections = self.windows.tcp_connections(self.process.pid)
            rooms = {self.endpoints[(canonical_address(c.remote_address), c.remote_port)].id
                     for c in connections if (canonical_address(c.remote_address), c.remote_port) in self.endpoints}
            if len(rooms) > 1:
                return {**result, "error": "multiple_target_connections"}
            result["connection_observed"] = True
            room = next((r for r in self.rooms() if r.id in rooms), None)
            if room:
                result.update(room_id=room.id, room_names=room.server_names)
            # A result baseline must be observed before arming a target match.
            self.memory.reset_budget()
            native = self.reader.poll()
            if not self.baseline_ready and self.reader.baseline_ready:
                self.baseline_report = native
                self.baseline_ready = True
            elif self.baseline_ready and native is not None:
                result["report"] = native
            result.update(baseline_ready=self.baseline_ready, baseline_report=self.baseline_report)
        except (OSError, ValueError):
            # Error strings are controlled reader codes; do not include OS
            # paths, raw memory, names, session IDs, or arbitrary exception text.
            result["error"] = "native_report_not_ready"
            result["baseline_ready"] = False
        except NativeReportError as exc:
            result["error"] = exc.code
            result["baseline_ready"] = False
        result["native_status"] = getattr(self.reader, "status", "")
        diagnostic = getattr(self.reader, "diagnostics", None)
        if diagnostic is not None:
            result["native_diagnostics"] = diagnostic()
        return result

    def close(self):
        self._reset()
        self.closed = True

