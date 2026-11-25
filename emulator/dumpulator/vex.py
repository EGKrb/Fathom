# Fathom addition to dumpulator: the VEX-encoded instructions (AVX, AVX2, FMA) executed correctly. Boost
# Software License 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# Unicorn (QEMU 5) runs a VEX.128 instruction it knows as the legacy SSE instruction of the same opcode:
# the second source (VEX.vvvv) is ignored and the destination is used as the first source, so that
# "vsubsd xmm4, xmm0, xmm1" computes xmm4 - xmm1. The C runtime of a machine with AVX2 takes such code
# (the FMA3 variants of log10, pow..., memcpy, strlen), and the results are wrong (printf("%g") prints
# garbage). So the VEX instructions are found before they run and replaced in memory by ud2:
# - a hook on each translation block generated (UC_HOOK_EDGE_GENERATED) decodes its instructions;
#   when it patches one, it stops the emulation, which restarts on the block, translated again;
# - the first block of each emu_start and the return address of an API hook are decoded too;
# the ud2 traps (UC_HOOK_INSN_INVALID): emulate() runs the original instruction. A VEX.128 instruction
# with a legacy SSE equivalent is encoded again as that instruction, with the first source copied to the
# destination beforehand (the operation of VEX), and run on a small unicorn of its own whose registers
# are copied from the emulator's; the bits 255:128 of the destination are cleared, as VEX.128 does.
# The other VEX instructions (256-bit, FMA, AVX2) go to avx.py. BMI1 / BMI2 (VEX-encoded general purpose
# instructions) are left to unicorn, which runs them right.

from typing import Dict, Optional

from capstone import CS_OP_MEM, CS_OP_REG, CS_AC_WRITE
from unicorn import Uc, UcError, UC_ARCH_X86, UC_MODE_32, UC_MODE_64
from unicorn import x86_const as X

UD2 = b"\x0f\x0b"
_SEGMENT_PREFIXES = {0x26, 0x2E, 0x36, 0x3E, 0x64, 0x65}
_BRANCHES = {"jmp", "call", "ret", "retf", "iret", "iretd", "iretq", "int", "int3", "syscall", "sysenter", "ud2", "hlt",
             "loop", "loope", "loopne", "jrcxz", "jecxz"}
# VEX instructions without a legacy SSE encoding (avx.py, or unsupported); BMI is left to unicorn
_VEX_ONLY = {(2, op) for op in (0x0C, 0x0D, 0x0E, 0x0F, 0x13, 0x16, 0x18, 0x19, 0x1A, 0x2C, 0x2D, 0x2E, 0x2F, 0x36,
                               0x45, 0x46, 0x47, 0x58, 0x59, 0x5A, 0x78, 0x79, 0x8C, 0x8E, 0x90, 0x91, 0x92, 0x93)}
_VEX_ONLY |= {(2, op) for op in range(0x96, 0xC0)}
_VEX_ONLY |= {(3, op) for op in (0x00, 0x01, 0x02, 0x04, 0x05, 0x06, 0x18, 0x19, 0x1D, 0x38, 0x39, 0x46, 0x48, 0x49)}
_VEX_ONLY |= {(3, op) for op in range(0x5C, 0x80)}
_BMI = {(2, op) for op in range(0xF0, 0xF8)} | {(3, 0xF0)}
_BLENDV = {0x4A: 0x14, 0x4B: 0x15, 0x4C: 0x10}       # vblendvps / vblendvpd / vpblendvb -> 66 0F 38 xx
_CODE, _DATA = 0x10000, 0x20000


def vex_fields(code: bytes, x64: bool) -> Optional[dict]:
    """The fields of a VEX-encoded instruction (after segment prefixes), or None."""
    i = 0
    while i < len(code) and code[i] in _SEGMENT_PREFIXES:
        i += 1
    if i + 3 > len(code) or code[i] not in (0xC4, 0xC5):
        return None
    if not x64 and (code[i + 1] & 0xC0) != 0xC0:
        return None    # LES / LDS in 32-bit mode
    if code[i] == 0xC5:
        b1 = code[i + 1]
        fields = dict(R=(~b1 >> 7) & 1, X=0, B=0, map=1, W=0, vvvv=(~b1 >> 3) & 15, L=(b1 >> 2) & 1, pp=b1 & 3,
                      opcode=code[i + 2], rest=i + 3)
    else:
        b1, b2 = code[i + 1], code[i + 2]
        fields = dict(R=(~b1 >> 7) & 1, X=(~b1 >> 6) & 1, B=(~b1 >> 5) & 1, map=b1 & 31, W=(b2 >> 7) & 1,
                      vvvv=(~b2 >> 3) & 15, L=(b2 >> 2) & 1, pp=b2 & 3, opcode=code[i + 3], rest=i + 4)
    if not x64:
        fields["R"] = fields["X"] = fields["B"] = 0
        fields["vvvv"] &= 7
    fields["prefix"] = i
    return fields


def needs_patch(code: bytes, x64: bool) -> bool:
    fields = vex_fields(code, x64)
    return fields is not None and (fields["map"], fields["opcode"]) not in _BMI


class VexPatcher:
    """The VEX instructions found in the code run, replaced by ud2 (original bytes by address)."""

    def __init__(self, dp):
        self.dp = dp
        self.original: Dict[int, bytes] = {}
        self.restart = False
        self.hook = None

    def install(self):
        from unicorn import UC_HOOK_EDGE_GENERATED
        self.hook = self.dp._uc.hook_add(UC_HOOK_EDGE_GENERATED, self._on_block)

    def _on_block(self, uc, current, previous, data):
        try:    # most blocks have no VEX prefix byte at all: nothing to decode
            code = uc.mem_read(current.pc, current.size)
        except Exception:
            return
        if 0xC4 not in code and 0xC5 not in code:
            return
        if self.scan(current.pc, current.size):
            self.restart = True
            uc.emu_stop()

    def scan(self, start: int, size: int = 0) -> bool:
        """Decodes the instructions of a block (size bytes, or up to its first branch) and patches its VEX
        instructions; True when one was patched."""
        dp = self.dp
        try:
            code = bytes(dp.read(start, size or 256))
        except Exception:
            if size:
                return False
            try:
                code = bytes(dp.read(start, 16))
            except Exception:
                return False
        if b"\xc4" not in code and b"\xc5" not in code:     # no VEX prefix byte: nothing to patch
            return False
        patched = False
        x64 = dp._x64
        offset = 0
        while offset < len(code):
            address = start + offset
            known = self.original.get(address)
            if known is not None:       # already patched: its ud2 ends a block
                offset += len(known)
                if not size:
                    break
                continue
            ins = next(dp.cs.disasm(code[offset:offset + 16], address, 1), None)
            if ins is None:
                break
            raw = bytes(ins.bytes)
            if needs_patch(raw, x64):
                self.original[address] = raw
                dp._uc.mem_write(address, UD2)
                patched = True
            offset += ins.size
            mnemonic = ins.mnemonic
            if not size and (mnemonic in _BRANCHES or mnemonic.startswith(("j", "ret", "call", "loop"))):
                break
        end = start + offset
        if patched:
            dp._uc.ctl_remove_cache(start, max(end, start + 1))
        return patched

    def instruction(self, address: int) -> Optional[bytes]:
        """The original bytes of a patched instruction whose ud2 is still there."""
        raw = self.original.get(address)
        if raw is None:
            return None
        try:
            if bytes(self.dp.read(address, 2)) != UD2:
                del self.original[address]  # the program rewrote its code
                return None
        except Exception:
            return None
        return raw


# ---------------------------------------------------------------------------------- the helper unicorn
_HELPERS: Dict[bool, Uc] = {}


def _helper(x64: bool) -> Uc:
    uc = _HELPERS.get(x64)
    if uc is None:
        uc = Uc(UC_ARCH_X86, UC_MODE_64 if x64 else UC_MODE_32)
        uc.mem_map(_CODE, 0x1000)
        uc.mem_map(_DATA, 0x1000)
        uc.reg_write(X.UC_X86_REG_CR4, uc.reg_read(X.UC_X86_REG_CR4) | (1 << 9) | (1 << 10))   # OSFXSR, OSXMMEXCPT
        uc.reg_write(X.UC_X86_REG_CR0, (uc.reg_read(X.UC_X86_REG_CR0) & ~4) | 2)
        _HELPERS[x64] = uc
    return uc


_GPR64 = ["rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi", "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"]
_GPR32 = ["eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi"]
_REG_OF_NAME = {}


def _uc_reg(name: str, x64: bool):
    """The unicorn register of a capstone name (32-bit general registers are written whole in 64-bit mode)."""
    key = (name, x64)
    if key not in _REG_OF_NAME:
        full = name
        if x64:
            widen = {"eax": "rax", "ecx": "rcx", "edx": "rdx", "ebx": "rbx", "esp": "rsp", "ebp": "rbp", "esi": "rsi",
                     "edi": "rdi"}
            full = widen.get(name, full)
            if name.startswith("r") and name.endswith("d") and name[1:-1].isdigit():
                full = name[:-1]
        if name in ("rflags", "eflags", "flags"):
            full = "eflags"
        _REG_OF_NAME[key] = getattr(X, "UC_X86_REG_" + full.upper(), None)
    return _REG_OF_NAME[key]


def _xmm(index: int):
    return getattr(X, f"UC_X86_REG_XMM{index}")


def _ymm(index: int):
    return getattr(X, f"UC_X86_REG_YMM{index}")


def _effective_address(dp, ins, op) -> int:
    mem = op.mem
    address = mem.disp
    if mem.base:
        name = ins.reg_name(mem.base)
        address += ins.address + ins.size if name in ("rip", "eip") else dp.regs[name]
    if mem.index:
        address += dp.regs[ins.reg_name(mem.index)] * mem.scale
    if mem.segment:
        segment = ins.reg_name(mem.segment)
        if segment in ("fs", "gs"):
            address += dp.regs[segment + "_base"]
    return address & dp.addr_mask


def emulate(dp, ins) -> bool:
    """A VEX.128 instruction run as its legacy SSE equivalent; False when it has none (or failed)."""
    raw = bytes(ins.bytes)
    x64 = dp._x64
    f = vex_fields(raw, x64)
    if f is None:
        return False
    uc, main = _helper(x64), dp._uc
    count = 16 if x64 else 8
    if f["map"] == 1 and f["opcode"] == 0x77:  # vzeroupper / vzeroall
        for k in range(count):
            value = main.reg_read(_xmm(k)) if not f["L"] else 0
            main.reg_write(_ymm(k), value)
        dp.regs.cip += ins.size
        return True
    if f["L"] or (f["map"], f["opcode"]) in _VEX_ONLY or (f["map"], f["opcode"]) in _BMI:
        return False
    blendv = f["map"] == 3 and f["opcode"] in _BLENDV
    if f["map"] == 1 and f["opcode"] == 0xC2 and raw[-1] > 7:
        return False    # the comparison predicates 8..31 of VEX
    if f["map"] == 1 and f["opcode"] == 0xF7:
        return False    # vmaskmovdqu writes at [rdi]
    rest = raw[f["rest"]:]
    if not rest:
        return False
    modrm = rest[0]
    mod, reg, rm = modrm >> 6, ((modrm >> 3) & 7) | (f["R"] << 3), (modrm & 7) | (f["B"] << 3)
    # the length of the original addressing bytes
    length = 1
    if mod != 3:
        if (modrm & 7) == 4:
            sib = rest[1]
            length += 1
            if mod == 0 and (sib & 7) == 5:
                length += 4
        elif mod == 0 and (modrm & 7) == 5:
            length += 4
        length += {1: 1, 2: 4}.get(mod, 0)
    immediate = rest[length:]
    operands = [op for op in ins.operands if op.type in (CS_OP_REG, CS_OP_MEM)]
    memory = next((op for op in ins.operands if op.type == CS_OP_MEM), None)
    ndd = f["map"] == 1 and f["opcode"] in (0x71, 0x72, 0x73) and f["pp"] == 1
    nds = not ndd and len(operands) >= 3
    used = {f["vvvv"], reg} | ({rm} if mod == 3 else set())
    if blendv:
        used.add((immediate[0] >> 4) & 15 if x64 else (immediate[0] >> 4) & 7)
    scratch = [k for k in range(count - 1, -1, -1) if k not in used and k != 0]
    # the registers of the helper: copies of the emulator's
    for k in range(count):
        uc.reg_write(_xmm(k), main.reg_read(_xmm(k)))
    gprs = _GPR64 if x64 else _GPR32
    for name in gprs:
        uc.reg_write(getattr(X, "UC_X86_REG_" + name.upper()), main.reg_read(getattr(X, "UC_X86_REG_" + name.upper())))
    uc.reg_write(X.UC_X86_REG_EFLAGS, main.reg_read(X.UC_X86_REG_EFLAGS))
    uc.reg_write(X.UC_X86_REG_MXCSR, main.reg_read(X.UC_X86_REG_MXCSR))
    helper_dest = reg
    new_reg, new_rm = reg, rm
    if ndd:     # vpsrlq xmm_v, xmm_rm, imm: the source in the destination, the destination in r/m
        uc.reg_write(_xmm(f["vvvv"]), main.reg_read(_xmm(rm)))
        new_rm = f["vvvv"]
        helper_dest = f["vvvv"]
    elif nds or blendv:
        if mod == 3 and rm == reg and rm != f["vvvv"]:
            # the second source is the destination: it moves to a free register first
            new_rm = scratch.pop(0)
            uc.reg_write(_xmm(new_rm), main.reg_read(_xmm(rm)))
        if blendv:
            mask = (immediate[0] >> 4) & (15 if x64 else 7)
            if reg == 0:
                helper_dest = new_reg = scratch.pop(0)
            if mod == 3 and new_rm == 0:
                new_rm = scratch.pop(0)
                uc.reg_write(_xmm(new_rm), main.reg_read(_xmm(0)))
            uc.reg_write(_xmm(helper_dest), main.reg_read(_xmm(f["vvvv"])))
            uc.reg_write(_xmm(0), main.reg_read(_xmm(mask)))
            immediate = b""
        else:
            uc.reg_write(_xmm(helper_dest), main.reg_read(_xmm(f["vvvv"])))
    # the memory operand: copied to the helper's data page, addressed absolutely
    address = None
    size = 0
    if memory is not None:
        address = _effective_address(dp, ins, memory)
        size = memory.size or 16
        try:
            uc.mem_write(_DATA, bytes(dp.read(address, size)))
        except Exception:
            if not memory.access & CS_AC_WRITE:
                return False
    # the legacy encoding
    out = bytearray()
    if f["pp"]:
        out.append({1: 0x66, 2: 0xF3, 3: 0xF2}[f["pp"]])
    rex = (f["W"] << 3) | (((new_reg >> 3) & 1) << 2) | ((((new_rm >> 3) & 1) if mod == 3 else 0))
    if rex and x64:
        out.append(0x40 | rex)
    out.append(0x0F)
    if f["map"] == 2:
        out.append(0x38)
    elif f["map"] == 3:
        out.append(0x3A if not blendv else 0x38)
    out.append(_BLENDV[f["opcode"]] if blendv else f["opcode"])
    if mod == 3:
        out.append(0xC0 | ((new_reg & 7) << 3) | (new_rm & 7))
    elif x64:
        out += bytes([((new_reg & 7) << 3) | 4, 0x25]) + _DATA.to_bytes(4, "little")
    else:
        out += bytes([((new_reg & 7) << 3) | 5]) + _DATA.to_bytes(4, "little")
    out += immediate
    try:
        uc.mem_write(_CODE, bytes(out))
        uc.emu_start(_CODE, _CODE + len(out), count=1)
    except UcError:
        return False
    # what the instruction writes, back in the emulator
    _, written = ins.regs_access()
    for register in written:
        name = ins.reg_name(register)
        if name.startswith("xmm"):
            index = int(name[3:])
            source = helper_dest if index == reg or (ndd and index == f["vvvv"]) else index
            main.reg_write(_ymm(index), uc.reg_read(_xmm(source)))     # VEX.128: bits 255:128 cleared
        else:
            target = _uc_reg(name, x64)
            if target is not None:
                main.reg_write(target, uc.reg_read(target))
    main.reg_write(X.UC_X86_REG_MXCSR, uc.reg_read(X.UC_X86_REG_MXCSR))
    if memory is not None and memory.access & CS_AC_WRITE:
        dp.write(address, bytes(uc.mem_read(_DATA, size)))
    dp.regs.cip += ins.size
    return True
