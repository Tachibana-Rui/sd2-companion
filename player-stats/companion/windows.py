"""Bounded, read-only Win32 access for the optional player companion.

Importing this module never opens a process or loads a Windows DLL. The only
process access rights requested here are QUERY_INFORMATION/VM_READ, or
QUERY_LIMITED_INFORMATION for discovery. No game code is executed or called.

TCP layouts follow Microsoft's GetExtendedTcpTable OWNER_PID_ALL structures;
region layouts follow MEMORY_BASIC_INFORMATION64. Explicit integer widths keep
the parsing helpers usable in synthetic tests on other operating systems.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import socket
import struct
from typing import NamedTuple


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_GUARD = 0x100
TCP_ESTABLISHED = 5
AF_INET_WIN = 2
AF_INET6_WIN = 23
MAX_ADDRESS = 0x00007FFFFFFFFFFF
MAX_TCP_TABLE_BYTES = 16 * 1024 * 1024
_READABLE_PROTECTIONS = frozenset((0x02, 0x04, 0x08, 0x20, 0x40, 0x80))
_DWORD = ctypes.c_uint32
_WORD = ctypes.c_uint16
_HANDLE = ctypes.c_void_p


class WindowsAccessError(OSError):
    """The target or a native API could not be read safely."""


class BudgetExceeded(WindowsAccessError):
    """A read/query limit was reached; this is not evidence of an empty match."""


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    creation_time_100ns: int  # Exact FILETIME ticks since 1601, not a float.
    exe_path: str
    exe_sha256: str
    image_base: int

    @property
    def path(self):
        return self.exe_path


@dataclass(frozen=True)
class TcpConnection:
    family: str
    local_address: str
    local_port: int
    remote_address: str
    remote_port: int
    state: int
    pid: int
    local_scope_id: int = 0
    remote_scope_id: int = 0


class MemoryRegion(NamedTuple):
    base_address: int
    size: int


class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64),
        ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", _DWORD),
        ("PartitionId", _WORD),
        ("_alignment1", _WORD),
        ("RegionSize", ctypes.c_uint64),
        ("State", _DWORD),
        ("Protect", _DWORD),
        ("Type", _DWORD),
        ("_alignment2", _DWORD),
    ]


class MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [(name, _DWORD) for name in (
        "dwState", "dwLocalAddr", "dwLocalPort", "dwRemoteAddr",
        "dwRemotePort", "dwOwningPid")]


class MIB_TCP6ROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("ucLocalAddr", ctypes.c_ubyte * 16), ("dwLocalScopeId", _DWORD),
        ("dwLocalPort", _DWORD), ("ucRemoteAddr", ctypes.c_ubyte * 16),
        ("dwRemoteScopeId", _DWORD), ("dwRemotePort", _DWORD),
        ("dwState", _DWORD), ("dwOwningPid", _DWORD),
    ]


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", _DWORD), ("high", _DWORD)]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", _DWORD), ("cntUsage", _DWORD), ("th32ProcessID", _DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", _DWORD),
        ("cntThreads", _DWORD), ("th32ParentProcessID", _DWORD),
        ("pcPriClassBase", ctypes.c_int32), ("dwFlags", _DWORD),
        ("szExeFile", _WORD * 260),
    ]


class _MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", _DWORD), ("th32ModuleID", _DWORD), ("th32ProcessID", _DWORD),
        ("GlblcntUsage", _DWORD), ("ProccntUsage", _DWORD),
        ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", _DWORD),
        ("hModule", _HANDLE), ("szModule", _WORD * 256),
        ("szExePath", _WORD * 260),
    ]


def _utf16_array(value):
    return bytes(value).decode("utf-16-le", "strict").split("\0", 1)[0]


def _native_error(operation, code=None):
    code = ctypes.get_last_error() if code is None else code
    # Avoid embedding process contents, account paths or addresses in errors.
    return WindowsAccessError(code, operation + " failed")


def _positive_int(value, name, maximum=MAX_ADDRESS):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise ValueError("Invalid " + name)
    return value


class _NativeAPI:
    def __init__(self):
        if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise WindowsAccessError("The companion requires 64-bit Python on Windows")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ip = ctypes.WinDLL("iphlpapi", use_last_error=True)
        self._bind(self.kernel, "OpenProcess", [_DWORD, ctypes.c_int32, _DWORD], _HANDLE)
        self._bind(self.kernel, "CloseHandle", [_HANDLE], ctypes.c_int32)
        self._bind(self.kernel, "GetProcessTimes", [_HANDLE] + [ctypes.POINTER(_FILETIME)] * 4, ctypes.c_int32)
        self._bind(self.kernel, "GetExitCodeProcess", [_HANDLE, ctypes.POINTER(_DWORD)], ctypes.c_int32)
        self._bind(self.kernel, "QueryFullProcessImageNameW", [_HANDLE, _DWORD, ctypes.c_wchar_p,
                                                             ctypes.POINTER(_DWORD)], ctypes.c_int32)
        self._bind(self.kernel, "CreateToolhelp32Snapshot", [_DWORD, _DWORD], _HANDLE)
        for name in ("Process32FirstW", "Process32NextW"):
            self._bind(self.kernel, name, [_HANDLE, ctypes.POINTER(_PROCESSENTRY32W)], ctypes.c_int32)
        for name in ("Module32FirstW", "Module32NextW"):
            self._bind(self.kernel, name, [_HANDLE, ctypes.POINTER(_MODULEENTRY32W)], ctypes.c_int32)
        self._bind(self.kernel, "ReadProcessMemory", [_HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                                     ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)], ctypes.c_int32)
        self._bind(self.kernel, "VirtualQueryEx", [_HANDLE, ctypes.c_void_p,
                                                  ctypes.POINTER(MEMORY_BASIC_INFORMATION64),
                                                  ctypes.c_size_t], ctypes.c_size_t)
        self._bind(self.ip, "GetExtendedTcpTable", [ctypes.c_void_p, ctypes.POINTER(_DWORD),
                                                   ctypes.c_int32, _DWORD, ctypes.c_int32, _DWORD], _DWORD)

    @staticmethod
    def _bind(dll, name, args, result):
        function = getattr(dll, name)
        function.argtypes = args
        function.restype = result

    def open_process(self, pid, access):
        handle = self.kernel.OpenProcess(access, False, pid)
        if not handle:
            raise _native_error("OpenProcess")
        return handle

    def close(self, handle):
        if not self.kernel.CloseHandle(handle):
            raise _native_error("CloseHandle")

    def creation_time(self, handle):
        created, exited, kernel, user = (_FILETIME() for _ in range(4))
        if not self.kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                           ctypes.byref(kernel), ctypes.byref(user)):
            raise _native_error("GetProcessTimes")
        return (created.high << 32) | created.low

    def alive(self, handle):
        status = _DWORD()
        if not self.kernel.GetExitCodeProcess(handle, ctypes.byref(status)):
            raise _native_error("GetExitCodeProcess")
        return status.value == 259

    def image_path(self, handle):
        buffer = ctypes.create_unicode_buffer(32768)
        length = _DWORD(len(buffer))
        if not self.kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            raise _native_error("QueryFullProcessImageNameW")
        return buffer.value

    def _snapshot(self, flags, pid):
        for _ in range(3):
            result = self.kernel.CreateToolhelp32Snapshot(flags, pid)
            if result != ctypes.c_void_p(-1).value:
                return result
            if ctypes.get_last_error() != 24:  # ERROR_BAD_LENGTH: module list changed.
                break
        raise _native_error("CreateToolhelp32Snapshot")

    def processes(self):
        handle = self._snapshot(0x02, 0)
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            valid = self.kernel.Process32FirstW(handle, ctypes.byref(entry))
            while valid:
                yield entry.th32ProcessID, _utf16_array(entry.szExeFile)
                valid = self.kernel.Process32NextW(handle, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES.
                raise _native_error("Process32NextW")
        finally:
            self.close(handle)

    def image_base(self, pid):
        handle = self._snapshot(0x08 | 0x10, pid)
        try:
            entry = _MODULEENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            if not self.kernel.Module32FirstW(handle, ctypes.byref(entry)):
                raise _native_error("Module32FirstW")
            return int(entry.modBaseAddr or 0)
        finally:
            self.close(handle)

    def read(self, handle, address, size):
        buffer = ctypes.create_string_buffer(size)
        actual = ctypes.c_size_t()
        if not self.kernel.ReadProcessMemory(handle, address, buffer, size, ctypes.byref(actual)):
            raise _native_error("ReadProcessMemory")
        if actual.value != size:
            raise WindowsAccessError("Incomplete process read")
        return buffer.raw

    def region(self, handle, address):
        result = MEMORY_BASIC_INFORMATION64()
        size = self.kernel.VirtualQueryEx(handle, address, ctypes.byref(result), ctypes.sizeof(result))
        if not size:
            if ctypes.get_last_error() == 87:  # Beyond the target address space.
                return None
            raise _native_error("VirtualQueryEx")
        if size != ctypes.sizeof(result):
            raise WindowsAccessError("Unexpected memory-region layout")
        return result

    def tcp_table(self, family):
        size = _DWORD()
        result = self.ip.GetExtendedTcpTable(None, ctypes.byref(size), False, family, 5, 0)
        if result not in (0, 122):
            raise _native_error("GetExtendedTcpTable", result)
        for _ in range(3):
            if not 4 <= size.value <= MAX_TCP_TABLE_BYTES:
                raise BudgetExceeded("TCP table exceeds the size limit")
            buffer = ctypes.create_string_buffer(size.value)
            result = self.ip.GetExtendedTcpTable(buffer, ctypes.byref(size), False, family, 5, 0)
            if result == 0:
                if size.value > len(buffer):
                    raise WindowsAccessError("TCP table changed while reading")
                return buffer.raw[:size.value]
            if result != 122:
                raise _native_error("GetExtendedTcpTable", result)
        raise WindowsAccessError("TCP table remained unstable")


@lru_cache(maxsize=1)
def _native_api():
    return _NativeAPI()


def _file_signature(value):
    # Windows Python versions can expose different creation/change timestamps
    # through stat(path) and fstat(handle). File ID/size/mtime agree across both.
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


@lru_cache(maxsize=16)
def _hash_executable(path, signature):
    if not 0 < signature[2] <= 1024 * 1024 * 1024:
        raise BudgetExceeded("Executable exceeds the hashing size limit")
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        if _file_signature(os.fstat(stream.fileno())) != signature:
            raise WindowsAccessError("Executable changed before hashing")
        consumed = 0
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            consumed += len(block)
            if consumed > signature[2]:
                raise WindowsAccessError("Executable grew while hashing")
            digest.update(block)
        if consumed != signature[2] or _file_signature(os.fstat(stream.fileno())) != signature:
            raise WindowsAccessError("Executable changed while hashing")
    return digest.hexdigest()


def executable_sha256(path):
    path = str(Path(path).resolve(strict=True))
    return _hash_executable(path, _file_signature(os.stat(path)))


def _process_info(pid, handle, api):
    created = api.creation_time(handle)
    path = str(Path(api.image_path(handle)).resolve(strict=True))
    digest = executable_sha256(path)
    base = _positive_int(api.image_base(pid), "image base")
    # The module snapshot used a PID. Verify it still refers to our open handle.
    if api.creation_time(handle) != created or not api.alive(handle):
        raise WindowsAccessError("Process changed during identification")
    return ProcessInfo(pid, created, path, digest, base)


def get_process_info(pid, *, _api=None):
    """Read executable identity; also useful for an explicitly owned test child."""
    _positive_int(pid, "PID", 0xFFFFFFFF)
    api = _api if _api is not None else _native_api()
    handle = api.open_process(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    try:
        return _process_info(pid, handle, api)
    finally:
        api.close(handle)


def list_game_processes(executable_path=None, expected_sha256=None, *, _api=None):
    """Discover game identities without reading their virtual memory.

    A process that exits or denies query access is omitted. An optional path and
    hash make discovery strict; they do not alter the supported native schema.
    """
    api = _api if _api is not None else _native_api()
    expected_path = os.path.normcase(str(Path(executable_path).resolve())) if executable_path else None
    if expected_sha256 is not None and (not isinstance(expected_sha256, str) or len(expected_sha256) != 64 or
                                       any(c not in "0123456789abcdef" for c in expected_sha256)):
        raise ValueError("Invalid executable fingerprint")
    found = []
    for pid, name in api.processes():
        if name.casefold() != "steeldivision2.exe":
            continue
        try:
            process = get_process_info(pid, _api=api)
        except OSError:
            continue
        if expected_path and os.path.normcase(process.exe_path) != expected_path:
            continue
        if expected_sha256 and process.exe_sha256 != expected_sha256:
            continue
        found.append(process)
    return sorted(found, key=lambda item: (item.pid, item.creation_time_100ns))


def _parse_tcp_table(data, family, pid, established_only=True):
    if not isinstance(data, bytes) or not 4 <= len(data) <= MAX_TCP_TABLE_BYTES:
        raise WindowsAccessError("Invalid TCP table length")
    row_type = MIB_TCPROW_OWNER_PID if family == AF_INET_WIN else MIB_TCP6ROW_OWNER_PID
    if family not in (AF_INET_WIN, AF_INET6_WIN):
        raise ValueError("Unsupported address family")
    count = struct.unpack_from("<I", data)[0]
    stride = ctypes.sizeof(row_type)
    if count > (len(data) - 4) // stride:
        raise WindowsAccessError("Truncated TCP table")
    connections = []
    for index in range(count):
        row = row_type.from_buffer_copy(data, 4 + index * stride)
        if row.dwOwningPid != pid or (established_only and row.dwState != TCP_ESTABLISHED):
            continue
        if family == AF_INET_WIN:
            local = socket.inet_ntop(socket.AF_INET, struct.pack("<I", row.dwLocalAddr))
            remote = socket.inet_ntop(socket.AF_INET, struct.pack("<I", row.dwRemoteAddr))
            local_scope = remote_scope = 0
        else:
            local = socket.inet_ntop(socket.AF_INET6, bytes(row.ucLocalAddr))
            remote = socket.inet_ntop(socket.AF_INET6, bytes(row.ucRemoteAddr))
            local_scope = socket.ntohl(row.dwLocalScopeId)
            remote_scope = socket.ntohl(row.dwRemoteScopeId)
        connections.append(TcpConnection(
            "ipv4" if family == AF_INET_WIN else "ipv6", local,
            socket.ntohs(row.dwLocalPort & 0xFFFF), remote,
            socket.ntohs(row.dwRemotePort & 0xFFFF), row.dwState, row.dwOwningPid,
            local_scope, remote_scope))
    return connections


def tcp_connections(pid, established_only=True, *, _api=None):
    """Return this PID's TCP endpoints. Endpoints are a trigger, not a match ID."""
    _positive_int(pid, "PID", 0xFFFFFFFF)
    api = _api if _api is not None else _native_api()
    result = []
    for family in (AF_INET_WIN, AF_INET6_WIN):
        result.extend(_parse_tcp_table(api.tcp_table(family), family, pid, established_only))
    return result


class WindowsMemory:
    """A fixed process handle with bounded reads and private-region discovery.

    Pass a ProcessInfo to require its exact PID/start-time/path/hash/base tuple.
    The handle cannot follow a reused PID. Failed/partial reads consume budget.
    Regions are (base_address, size) named tuples. Scans default to committed,
    readable MEM_PRIVATE pages; callers may explicitly include mapped images.
    """
    def __init__(self, process, image_base=None, exe_sha256=None, *,
                 expected_creation_time=None, max_read_bytes=64 * 1024 * 1024,
                 max_single_read=4 * 1024 * 1024, max_regions=65536,
                 max_region_queries=262144, max_address=MAX_ADDRESS, _api=None):
        expected = process if isinstance(process, ProcessInfo) else None
        pid = expected.pid if expected else process
        _positive_int(pid, "PID", 0xFFFFFFFF)
        for value, name in ((max_read_bytes, "read budget"), (max_single_read, "single read limit"),
                            (max_regions, "region limit"), (max_region_queries, "query limit"),
                            (max_address, "maximum address")):
            _positive_int(value, name)
        self._api = _api if _api is not None else _native_api()
        self._handle = None
        self.max_read_bytes = max_read_bytes
        self.max_single_read = max_single_read
        self.max_regions = max_regions
        self.max_region_queries = max_region_queries
        self.max_address = max_address
        self.bytes_read = self.region_queries = self.regions_returned = 0
        self.total_bytes_read = self.total_region_queries = 0
        handle = self._api.open_process(pid, PROCESS_QUERY_INFORMATION | PROCESS_VM_READ)
        try:
            info = _process_info(pid, handle, self._api)
            if expected is not None and info != expected:
                raise WindowsAccessError("Process identity changed")
            if expected_creation_time is not None and info.creation_time_100ns != expected_creation_time:
                raise WindowsAccessError("Process creation time changed")
            if image_base is not None and info.image_base != image_base:
                raise WindowsAccessError("Executable image base changed")
            if exe_sha256 is not None and info.exe_sha256 != exe_sha256:
                raise WindowsAccessError("Executable fingerprint changed")
            self.process = info
            self.pid = info.pid
            self.image_base = info.image_base
            self.exe_sha256 = info.exe_sha256
            self.creation_time_100ns = info.creation_time_100ns
            self._handle = handle
        except BaseException:
            self._api.close(handle)
            raise

    def _check_open(self):
        if self._handle is None:
            raise WindowsAccessError("Process reader is closed")

    def read(self, address, size):
        self._check_open()
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("Invalid read size")
        if size == 0:
            return b""
        _positive_int(address, "read address", self.max_address)
        if size > self.max_single_read or size > self.max_read_bytes - self.bytes_read:
            raise BudgetExceeded("Process read budget exhausted")
        if address + size - 1 > self.max_address:
            raise WindowsAccessError("Read address is outside the user address space")
        self.bytes_read += size
        self.total_bytes_read += size
        result = self._api.read(self._handle, address, size)
        if not isinstance(result, bytes) or len(result) != size:
            raise WindowsAccessError("Incomplete process read")
        return result

    def iter_readable_regions(self, *, private_only=True):
        self._check_open()
        address = 0
        while address <= self.max_address:
            self._check_open()
            if self.region_queries >= self.max_region_queries:
                raise BudgetExceeded("Memory-region query budget exhausted")
            self.region_queries += 1
            self.total_region_queries += 1
            region = self._api.region(self._handle, address)
            if region is None:
                break
            base, size = int(region.BaseAddress), int(region.RegionSize)
            end = base + size
            if size <= 0 or base > address or end <= address or end > (1 << 64):
                raise WindowsAccessError("Invalid memory-region bounds")
            if (region.State == MEM_COMMIT and not region.Protect & PAGE_GUARD and
                    region.Protect & 0xFF in _READABLE_PROTECTIONS and
                    (not private_only or region.Type == MEM_PRIVATE)):
                if self.regions_returned >= self.max_regions:
                    raise BudgetExceeded("Memory-region result budget exhausted")
                self.regions_returned += 1
                yield MemoryRegion(base, min(end, self.max_address + 1) - base)
            address = end

    def reset_budget(self):
        """Begin a caller-controlled polling cycle; preserve lifetime counters."""
        self._check_open()
        self.bytes_read = self.region_queries = self.regions_returned = 0

    def close(self):
        handle, self._handle = self._handle, None
        if handle is not None:
            self._api.close(handle)

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
