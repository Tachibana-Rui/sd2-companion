"""Observe actual replay I/O by the selected game process using a dedicated ETW session.

No filesystem timestamps, recent-file lists, hooks, process writes, or global
kernel logger are used. A Read/Write event is evidence of I/O, not by itself
proof that the user played a replay: consumers must reject header enumeration.
TDH resolves event properties by name rather than guessed payload offsets.

Win32 ABI references: Microsoft Learn StartTraceW, EnableTraceEx2,
EVENT_TRACE_LOGFILEW, EVENT_RECORD, and PROPERTY_DATA_DESCRIPTOR. Provider
keywords/templates were additionally checked against Windows' registered
Microsoft-Windows-Kernel-File manifest (events 10--16, versions 0/1).
"""
from __future__ import annotations

from collections import deque
import ctypes
from dataclasses import dataclass
import math
import ntpath
import os
import threading
import time
import uuid


SESSION_NAME = "SD2CompanionReplayFiles"
PROVIDER_ID = "edd08927-9cc4-4e65-b970-c2560fb5c289"
_PROVIDER_BYTES = uuid.UUID(PROVIDER_ID).bytes_le
_U8, _U16, _U32, _U64 = ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint32, ctypes.c_uint64
_P = ctypes.c_void_p
_CALLBACK = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_FILETIME_EPOCH = 11644473600


class FileMonitorError(OSError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FileReadEvent:
    process_key: str
    path: str
    observed_at: float
    sequence: int
    byte_offset: int
    read_size: int
    operation: str = "read"

    @property
    def offset(self):
        return self.byte_offset

    @property
    def size(self):
        return self.read_size


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", _U32), ("Data2", _U16), ("Data3", _U16), ("Data4", _U8 * 8)]


class _WNODE_HEADER(ctypes.Structure):
    _fields_ = [("BufferSize", _U32), ("ProviderId", _U32), ("HistoricalContext", _U64),
                ("TimeStamp", _U64), ("Guid", _GUID), ("ClientContext", _U32), ("Flags", _U32)]


class _EVENT_TRACE_PROPERTIES(ctypes.Structure):
    _fields_ = [("Wnode", _WNODE_HEADER)] + [(name, _U32) for name in (
        "BufferSize", "MinimumBuffers", "MaximumBuffers", "MaximumFileSize", "LogFileMode",
        "FlushTimer", "EnableFlags", "AgeLimit", "NumberOfBuffers", "FreeBuffers", "EventsLost",
        "BuffersWritten", "LogBuffersLost", "RealTimeBuffersLost")] + [
            ("LoggerThreadId", _P), ("LogFileNameOffset", _U32), ("LoggerNameOffset", _U32)]


class _EVENT_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("Id", _U16), ("Version", _U8), ("Channel", _U8), ("Level", _U8),
                ("Opcode", _U8), ("Task", _U16), ("Keyword", _U64)]


class _EVENT_HEADER(ctypes.Structure):
    _fields_ = [("Size", _U16), ("HeaderType", _U16), ("Flags", _U16), ("EventProperty", _U16),
                ("ThreadId", _U32), ("ProcessId", _U32), ("TimeStamp", _U64),
                ("ProviderId", _GUID), ("EventDescriptor", _EVENT_DESCRIPTOR),
                ("ProcessorTime", _U64), ("ActivityId", _GUID)]


class _EVENT_RECORD(ctypes.Structure):
    _fields_ = [("EventHeader", _EVENT_HEADER), ("BufferContext", _U32),
                ("ExtendedDataCount", _U16), ("UserDataLength", _U16),
                ("ExtendedData", _P), ("UserData", _P), ("UserContext", _P)]


class _EVENT_TRACE_HEADER(ctypes.Structure):
    _fields_ = [("Size", _U16), ("FieldTypeFlags", _U16), ("Version", _U32),
                ("ThreadId", _U32), ("ProcessId", _U32), ("TimeStamp", _U64),
                ("Guid", _GUID), ("ProcessorTime", _U64)]


class _EVENT_TRACE(ctypes.Structure):
    _fields_ = [("Header", _EVENT_TRACE_HEADER), ("InstanceId", _U32), ("ParentInstanceId", _U32),
                ("ParentGuid", _GUID), ("MofData", _P), ("MofLength", _U32), ("ClientContext", _U32)]


class _TRACE_LOGFILE_HEADER(ctypes.Structure):
    _fields_ = [("BufferSize", _U32), ("Version", _U32), ("ProviderVersion", _U32),
                ("NumberOfProcessors", _U32), ("EndTime", _U64), ("TimerResolution", _U32),
                ("MaximumFileSize", _U32), ("LogFileMode", _U32), ("BuffersWritten", _U32),
                ("StartBuffers", _U32), ("PointerSize", _U32), ("EventsLost", _U32),
                ("CpuSpeedInMHz", _U32), ("LoggerName", _P), ("LogFileName", _P),
                ("TimeZone", _U8 * 172), ("BootTime", _U64), ("PerfFreq", _U64),
                ("StartTime", _U64), ("ReservedFlags", _U32), ("BuffersLost", _U32)]


class _EVENT_TRACE_LOGFILEW(ctypes.Structure):
    _fields_ = [("LogFileName", ctypes.c_wchar_p), ("LoggerName", ctypes.c_wchar_p),
                ("CurrentTime", _U64), ("BuffersRead", _U32), ("ProcessTraceMode", _U32),
                ("CurrentEvent", _EVENT_TRACE), ("LogfileHeader", _TRACE_LOGFILE_HEADER),
                ("BufferCallback", _P), ("BufferSize", _U32), ("Filled", _U32),
                ("EventsLost", _U32), ("EventRecordCallback", _P), ("IsKernelTrace", _U32),
                ("Context", _P)]


class _PROPERTY_DATA_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("PropertyName", _U64), ("ArrayIndex", _U32), ("Reserved", _U32)]


def _error(code):
    if code in (5, 1314):
        return FileMonitorError("file_monitor_requires_elevation")
    if code == 183:
        return FileMonitorError("file_monitor_session_already_running")
    return FileMonitorError("file_monitor_windows_api_failed")


def _replay_path(value, device_paths=()):
    """Translate an absolute NT path without opening or guessing a file."""
    if not isinstance(value, str) or not value or len(value) > 32767 or any(ord(c) < 32 for c in value):
        return None
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith(("\\\\?\\", "\\??\\")):
        value = value[4:]
    elif value.casefold().startswith("\\device\\mup\\"):
        value = "\\\\" + value[12:]
    elif value.casefold().startswith("\\device\\"):
        for prefix, drive in sorted(device_paths, key=lambda p: len(p[0]), reverse=True):
            if value.casefold().startswith(prefix.casefold() + "\\"):
                value = drive + value[len(prefix):]
                break
        else:
            return None
    value = ntpath.normpath(value)
    drive, tail = ntpath.splitdrive(value)
    if not drive or not tail.startswith("\\") or ntpath.splitext(value)[1].lower() not in (".rpl3", ".rpl"):
        return None
    if value.startswith("\\\\.\\"):
        return None
    return value


class _NativeSession:
    def __init__(self, api, name, accept, on_event, on_loss):
        self.api, self.name, self.accept = api, name, accept
        self.on_event, self.on_loss = on_event, on_loss
        self.controller = _U64(0)
        self.consumer = None
        self.stopping = False
        name_bytes = name.encode("utf-16-le") + b"\0\0"
        self.properties_buffer = ctypes.create_string_buffer(ctypes.sizeof(_EVENT_TRACE_PROPERTIES) + len(name_bytes))
        self.properties = _EVENT_TRACE_PROPERTIES.from_buffer(self.properties_buffer)
        self.properties.Wnode.BufferSize = len(self.properties_buffer)
        self.properties.Wnode.Flags = 0x00020000  # WNODE_FLAG_TRACED_GUID
        self.properties.Wnode.ClientContext = 2  # System time; no RAW_TIMESTAMP mode.
        self.properties.BufferSize = 64
        self.properties.MinimumBuffers = 4
        self.properties.MaximumBuffers = 64
        self.properties.LogFileMode = 0x100 | 0x10000000  # REAL_TIME | NO_PER_PROCESSOR_BUFFERING
        self.properties.FlushTimer = 1
        self.properties.LoggerNameOffset = ctypes.sizeof(_EVENT_TRACE_PROPERTIES)
        self.properties.Wnode.Guid = _GUID.from_buffer_copy(uuid.uuid4().bytes_le)
        status = api.advapi.StartTraceW(ctypes.byref(self.controller), name, ctypes.byref(self.properties))
        if status:
            raise _error(status)
        try:
            provider = _GUID.from_buffer_copy(_PROVIDER_BYTES)
            # Filename + general file I/O + create + read + write. Only replay
            # filenames and the selected PID's I/O survive callback filtering.
            status = api.advapi.EnableTraceEx2(self.controller, ctypes.byref(provider), 1, 4, 0x3B0, 0, 2000, None)
            if status:
                raise _error(status)
            self.record_callback = _CALLBACK(None, ctypes.POINTER(_EVENT_RECORD))(self._record)
            self.buffer_callback = _CALLBACK(_U32, ctypes.POINTER(_EVENT_TRACE_LOGFILEW))(self._buffer)
            self.log = _EVENT_TRACE_LOGFILEW()
            self.log.LoggerName = name
            self.log.ProcessTraceMode = 0x100 | 0x10000000  # REAL_TIME | EVENT_RECORD
            self.log.EventRecordCallback = ctypes.cast(self.record_callback, _P)
            self.log.BufferCallback = ctypes.cast(self.buffer_callback, _P)
            handle = api.advapi.OpenTraceW(ctypes.byref(self.log))
            if handle == 0xFFFFFFFFFFFFFFFF:
                raise _error(ctypes.get_last_error())
            self.consumer = _U64(handle)
        except BaseException:
            self.close()
            raise

    def _buffer(self, pointer):
        try:
            log = pointer.contents
            if log.EventsLost or log.LogfileHeader.BuffersLost or log.LogfileHeader.EventsLost:
                self.on_loss("file_monitor_events_lost")
        except Exception:
            self.on_loss("file_monitor_event_decode_failed")
        return 0 if self.stopping else 1

    def _record(self, pointer):
        try:
            header = pointer.contents.EventHeader
            if bytes(header.ProviderId) != _PROVIDER_BYTES:
                return
            event_id = header.EventDescriptor.Id
            pid = header.ProcessId
            timestamp = header.TimeStamp / 10_000_000 - _FILETIME_EPOCH
            if not self.accept(event_id, pid, timestamp):
                # Some asynchronous I/O is emitted in System's context. Only
                # use the issuing thread when Windows can prove its current
                # owner and that the thread predates the event (TIDs reuse).
                if (event_id not in (15, 16) or pid not in (0, 4, 0xFFFFFFFF)
                        or not self.accept(0, 0, timestamp)):
                    return
                tid = self.api.property(pointer, "IssuingThreadId")
                if tid is None:
                    tid = self.api.property(pointer, "ThreadId")
                pid = self.api.thread_owner(tid, header.TimeStamp)
                if pid is None or not self.accept(event_id, pid, timestamp):
                    return
            raw = {"event_id": event_id, "pid": pid, "timestamp": timestamp}
            for name in (("FileKey", "FileName") if event_id in (10, 11) else
                         ("FileObject", "FileName") if event_id == 12 else
                         ("FileObject", "FileKey") if event_id in (13, 14) else
                         ("FileObject", "FileKey", "ByteOffset", "IOSize")):
                value = self.api.property(pointer, name)
                if value is not None:
                    raw[name] = value
            self.on_event(raw)
        except FileMonitorError as exc:
            self.on_loss(exc.code)
        except Exception:
            self.on_loss("file_monitor_event_decode_failed")

    def consume(self):
        consumer = self.consumer
        if consumer is None:
            return
        handles = (_U64 * 1)(consumer.value)
        status = self.api.advapi.ProcessTrace(handles, 1, None, None)
        if not self.stopping:
            self.on_loss("file_monitor_trace_ended" if not status else "file_monitor_trace_failed")

    def close(self):
        self.stopping = True
        if self.controller.value:
            self.api.advapi.ControlTraceW(self.controller, None, ctypes.byref(self.properties), 1)
            self.controller.value = 0
        if self.consumer is not None:
            self.api.advapi.CloseTrace(self.consumer)
            self.consumer = None


class _NativeAPI:
    def __init__(self):
        if os.name != "nt" or ctypes.sizeof(_P) != 8:
            raise FileMonitorError("file_monitor_requires_64bit_windows")
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.tdh = ctypes.WinDLL("tdh", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        for dll, name, args, result in (
            (self.advapi, "StartTraceW", [ctypes.POINTER(_U64), ctypes.c_wchar_p, ctypes.POINTER(_EVENT_TRACE_PROPERTIES)], _U32),
            (self.advapi, "EnableTraceEx2", [_U64, ctypes.POINTER(_GUID), _U32, _U8, _U64, _U64, _U32, _P], _U32),
            (self.advapi, "OpenTraceW", [ctypes.POINTER(_EVENT_TRACE_LOGFILEW)], _U64),
            (self.advapi, "ProcessTrace", [ctypes.POINTER(_U64), _U32, _P, _P], _U32),
            (self.advapi, "CloseTrace", [_U64], _U32),
            (self.advapi, "ControlTraceW", [_U64, ctypes.c_wchar_p, ctypes.POINTER(_EVENT_TRACE_PROPERTIES), _U32], _U32),
            (self.tdh, "TdhGetPropertySize", [ctypes.POINTER(_EVENT_RECORD), _U32, _P, _U32, ctypes.POINTER(_PROPERTY_DATA_DESCRIPTOR), ctypes.POINTER(_U32)], _U32),
            (self.tdh, "TdhGetProperty", [ctypes.POINTER(_EVENT_RECORD), _U32, _P, _U32, ctypes.POINTER(_PROPERTY_DATA_DESCRIPTOR), _U32, _P], _U32),
            (self.kernel, "QueryDosDeviceW", [ctypes.c_wchar_p, ctypes.c_wchar_p, _U32], _U32),
            (self.kernel, "OpenThread", [_U32, ctypes.c_int32, _U32], _P),
            (self.kernel, "GetProcessIdOfThread", [_P], _U32),
            (self.kernel, "GetThreadTimes", [_P] + [ctypes.POINTER(_U64)] * 4, ctypes.c_int32),
            (self.kernel, "CloseHandle", [_P], ctypes.c_int32),
        ):
            function = getattr(dll, name)
            function.argtypes, function.restype = args, result
        self.device_paths = []
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            buffer = ctypes.create_unicode_buffer(32768)
            if self.kernel.QueryDosDeviceW(letter + ":", buffer, len(buffer)):
                self.device_paths.append((buffer.value, letter + ":"))

    def thread_owner(self, tid, event_time_100ns):
        if type(tid) is not int or not 0 < tid < 2**32:
            return None
        handle = self.kernel.OpenThread(0x0800, False, tid)  # THREAD_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            created, exited, kernel, user = (_U64() for _ in range(4))
            if not self.kernel.GetThreadTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                              ctypes.byref(kernel), ctypes.byref(user)):
                return None
            if created.value > event_time_100ns:
                return None
            return self.kernel.GetProcessIdOfThread(handle) or None
        finally:
            self.kernel.CloseHandle(handle)

    def property(self, pointer, name):
        text = ctypes.create_unicode_buffer(name)
        descriptor = _PROPERTY_DATA_DESCRIPTOR(ctypes.addressof(text), 0xFFFFFFFF, 0)
        size = _U32()
        status = self.tdh.TdhGetPropertySize(pointer, 0, None, 1, ctypes.byref(descriptor), ctypes.byref(size))
        if status == 1168:
            return None
        if status or not 0 < size.value <= 65536:
            raise FileMonitorError("file_monitor_event_schema_unsupported")
        buffer = ctypes.create_string_buffer(size.value)
        status = self.tdh.TdhGetProperty(pointer, 0, None, 1, ctypes.byref(descriptor), size, buffer)
        if status:
            raise FileMonitorError("file_monitor_event_schema_unsupported")
        data = buffer.raw
        if name == "FileName":
            if len(data) % 2:
                raise FileMonitorError("file_monitor_event_schema_unsupported")
            return data.decode("utf-16-le", "strict").rstrip("\0")
        if len(data) not in (4, 8):
            raise FileMonitorError("file_monitor_event_schema_unsupported")
        return int.from_bytes(data, "little")

    def start(self, name, accept, on_event, on_loss):
        return _NativeSession(self, name, accept, on_event, on_loss)


class FileAccessMonitor:
    """Bounded producer/consumer queue. Start before the user opens a replay.

    ``set_process`` must receive a PID together with the caller's verified
    PID/creation-time key. Changing that key clears pending events and per-
    process object mappings. The global FileKey/name table follows NameCreate /
    NameDelete independently of process identity. Events queued by ETW before
    that selection are not reassigned to the new process.
    Fatal event loss invalidates the whole queue; it is never silently dropped.
    """
    def __init__(self, *, max_events=8192, max_names=4096, clock=time.time, _api=None):
        if any(type(value) is not int or value < 1 for value in (max_events, max_names)):
            raise ValueError("invalid_file_monitor_limits")
        self.max_events, self.max_names, self.clock = max_events, max_names, clock
        self.api = _api
        self.status = "file_monitor_not_started"
        self.session = self.thread = None
        self._lock = threading.RLock()
        self._events = deque()
        self._names, self._objects = {}, {}
        self._pid = self._key = None
        self._selected_at = float("inf")
        self._sequence = 0
        self.unresolved_reads = 0

    def start(self):
        if self.status != "file_monitor_not_started":
            raise FileMonitorError("file_monitor_already_started")
        try:
            if self.api is None:
                self.api = _NativeAPI()
            self.session = self.api.start(SESSION_NAME, self._accept, self._event, self._fail)
            self.status = "file_monitor_running"
            self.thread = threading.Thread(target=self.session.consume, name="sd2-replay-file-events", daemon=True)
            self.thread.start()
        except (OSError, ValueError) as exc:
            self.status = exc.code if isinstance(exc, FileMonitorError) else "file_monitor_start_failed"
            if self.session is not None:
                self.session.close()
                self.session = None
            raise FileMonitorError(self.status) from None

    def set_process(self, pid, process_key):
        if pid is not None and (type(pid) is not int or not 0 < pid < 2**32 or not isinstance(process_key, str) or not process_key):
            raise ValueError("invalid_file_monitor_process")
        if pid is None and process_key is not None:
            raise ValueError("invalid_file_monitor_process")
        with self._lock:
            if (pid, process_key) == (self._pid, self._key):
                return
            self._pid, self._key = pid, process_key
            self._selected_at = self.clock() if pid is not None else float("inf")
            self._events.clear()
            self._objects.clear()

    def _accept(self, event_id, pid, timestamp):
        if event_id == 0:  # Is a target selected for an issuing-thread lookup?
            return self.status == "file_monitor_running" and self._pid is not None and timestamp >= self._selected_at
        if self.status != "file_monitor_running" or event_id not in (10, 11, 12, 13, 14, 15, 16):
            return False
        # Name records can be emitted by a system thread. They only provide a
        # path lookup; an actual target PID read/write is still required.
        return event_id in (10, 11) or (pid == self._pid and timestamp >= self._selected_at)

    def _fail(self, code):
        with self._lock:
            if self.status == "file_monitor_closed":
                return
            self.status = code
            self._events.clear()
            self._objects.clear()
            self._names.clear()

    def _remember(self, mapping, key, path):
        if type(key) is not int or not 0 < key < 2**64:
            return
        mapping.pop(key, None)
        if path is not None:
            if len(mapping) >= self.max_names:
                self._fail("file_monitor_name_capacity_exceeded")
                return
            mapping[key] = path

    def _event(self, raw):
        with self._lock:
            event_id, pid, timestamp = raw["event_id"], raw["pid"], raw["timestamp"]
            if not self._accept(event_id, pid, timestamp):
                return
            if event_id in (10, 11, 12):
                path = _replay_path(raw.get("FileName"), getattr(self.api, "device_paths", ()))
                mapping, key = (self._names, raw.get("FileKey")) if event_id in (10, 11) else (self._objects, raw.get("FileObject"))
                self._remember(mapping, key, None if event_id == 11 else path)
                return
            if event_id in (13, 14):
                self._objects.pop(raw.get("FileObject"), None)
                return
            path = self._objects.get(raw.get("FileObject")) or self._names.get(raw.get("FileKey"))
            if path is None:
                self.unresolved_reads += 1
                return
            offset, size = raw.get("ByteOffset"), raw.get("IOSize")
            if (type(offset) is not int or not 0 <= offset < 2**64 or type(size) is not int
                    or not 0 < size < 2**32 or offset + size > 2**64
                    or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp)):
                self._fail("file_monitor_event_schema_unsupported")
                return
            if len(self._events) >= self.max_events:
                self._fail("file_monitor_events_lost")
                return
            self._sequence += 1
            self._events.append(FileReadEvent(self._key, path, timestamp, self._sequence, offset, size,
                                              "read" if event_id == 15 else "write"))

    def poll(self):
        with self._lock:
            result = list(self._events) if self.status == "file_monitor_running" else []
            self._events.clear()
            return result

    def close(self):
        with self._lock:
            if self.status == "file_monitor_closed":
                return
            self.status = "file_monitor_closed"
            session, self.session = self.session, None
            self._events.clear()
            self._objects.clear()
            self._names.clear()
        if session is not None:
            session.close()
        if self.thread is not None:
            self.thread.join(timeout=2)
