"""Resolve a deliberately tiny subset of side-effect-free x64 getters.

This decodes bytes read from the fingerprint-checked game image. It never calls
or executes those bytes. Anything outside the explicit patterns is rejected.
"""
from __future__ import annotations

import struct


class GetterError(ValueError):
    pass


def _read(memory, address, size):
    data = memory.read(address, size)
    if len(data) != size:
        raise GetterError("getter_read_incomplete")
    return data


def _pointer(memory, address):
    value = struct.unpack("<Q", _read(memory, address, 8))[0]
    if not 0x10000 <= value < 0x0000800000000000:
        raise GetterError("getter_pointer_invalid")
    return value


def resolve_getter(memory, this, function_address, *, max_hops=8):
    """Return a native getter's address result without running game code.

    Accepted forms: mov/lea rax,[rcx+disp];ret, RIP-relative pointer/lea,
    optional endbr64, direct/indirect tail jumps, and the verified stackless
    sub-rcx/virtual-tail-jump adjustor. No calls, memory writes, stack
    operations, conditions, or arbitrary instructions are supported.
    """
    if not 0x10000 <= this < 0x0000800000000000:
        raise GetterError("getter_this_invalid")
    address = function_address
    visited = set()
    for _ in range(max_hops):
        if address in visited:
            raise GetterError("getter_cycle")
        visited.add(address)
        code = _read(memory, address, 16)
        if code.startswith(b"\xf3\x0f\x1e\xfa"):
            address += 4
            code = _read(memory, address, 16)
        if code.startswith(b"\xe9"):
            address += 5 + struct.unpack_from("<i", code, 1)[0]
            continue
        if code.startswith(b"\xeb"):
            address += 2 + struct.unpack_from("<b", code, 1)[0]
            continue
        if code.startswith(b"\xff\x25"):
            address = _pointer(memory, address + 6 + struct.unpack_from("<i", code, 2)[0])
            continue
        # The real localisation resource uses a secondary-interface thunk:
        # sub rcx,imm8; mov rax,[rcx]; jmp qword[rax+disp8]. Both immediates
        # are signed. In particular 0x80 means subtract(-128), i.e. add 128.
        # Only resolve the adjusted object and target; never execute the code.
        if code[:3] == b"\x48\x83\xe9" and code[4:10] == b"\x48\x8b\x01\x48\xff\x60":
            this -= struct.unpack_from("<b", code, 3)[0]
            if not 0x10000 <= this < 0x0000800000000000:
                raise GetterError("getter_this_invalid")
            vtable = _pointer(memory, this)
            address = _pointer(memory, vtable + struct.unpack_from("<b", code, 10)[0])
            continue
        if code[:4] == b"\x48\x8b\xc1\xc3":
            return this
        for opcode, dereference in ((b"\x48\x8b", True), (b"\x48\x8d", False)):
            if code[:2] != opcode:
                continue
            modrm = code[2]
            if modrm == 0x01 and code[3] == 0xC3:
                target = this
            elif modrm == 0x41 and code[4] == 0xC3:
                target = this + struct.unpack_from("<b", code, 3)[0]
            elif modrm == 0x81 and code[7] == 0xC3:
                target = this + struct.unpack_from("<i", code, 3)[0]
            elif modrm == 0x05 and code[7] == 0xC3:
                target = address + 7 + struct.unpack_from("<i", code, 3)[0]
            else:
                continue
            if not 0x10000 <= target < 0x0000800000000000:
                raise GetterError("getter_result_invalid")
            return _pointer(memory, target) if dereference else target
        raise GetterError("getter_instructions_unsupported")
    raise GetterError("getter_hop_limit")

