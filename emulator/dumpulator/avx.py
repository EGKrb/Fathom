# Fathom addition to dumpulator: AVX / AVX2 instructions in Python. Boost Software License 1.0,
# like the rest of dumpulator (LICENSE.dumpulator).
#
# Unicorn (QEMU 5) has no AVX. A process dumped on a CPU with AVX2 has its C runtime already set up
# for it (ntdll, ucrtbase and vcruntime choose their string and memory routines from CPUID when the
# process starts), so emulated code reaches VEX instructions in wcslen, memcpy, memchr... These are
# emulated here when unicorn reports them as invalid: the integer and logical forms those routines
# use, on 128-bit (xmm) and 256-bit (ymm) registers.

from capstone import CS_OP_REG
from capstone import x86_const as _x86


class _Ins:
    # X86_INS_* ids by name; -1 for a name this capstone version does not have
    def __getattr__(self, name):
        return getattr(_x86, "X86_INS_" + name, -1)


INS = _Ins()

_MASK128 = (1 << 128) - 1
_MASK256 = (1 << 256) - 1


def _lanes(value: int, width: int, count: int):
    mask = (1 << width) - 1
    return [(value >> (i * width)) & mask for i in range(count)]


def _join(lanes, width: int) -> int:
    result = 0
    for i, lane in enumerate(lanes):
        result |= (lane & ((1 << width) - 1)) << (i * width)
    return result


def _signed(value: int, width: int) -> int:
    return value - (1 << width) if value >> (width - 1) else value


_LOGIC = {
    INS.VPXOR: lambda a, b: a ^ b,
    INS.VXORPS: lambda a, b: a ^ b,
    INS.VXORPD: lambda a, b: a ^ b,
    INS.VPOR: lambda a, b: a | b,
    INS.VORPS: lambda a, b: a | b,
    INS.VORPD: lambda a, b: a | b,
    INS.VPAND: lambda a, b: a & b,
    INS.VANDPS: lambda a, b: a & b,
    INS.VANDPD: lambda a, b: a & b,
    INS.VPANDN: lambda a, b: ~a & b,
    INS.VANDNPS: lambda a, b: ~a & b,
    INS.VANDNPD: lambda a, b: ~a & b,
}

# lane-wise integer operations: instruction -> (lane width, operation on (a, b, width))
_LANEWISE = {
    INS.VPCMPEQB: (8, lambda a, b, w: (1 << w) - 1 if a == b else 0),
    INS.VPCMPEQW: (16, lambda a, b, w: (1 << w) - 1 if a == b else 0),
    INS.VPCMPEQD: (32, lambda a, b, w: (1 << w) - 1 if a == b else 0),
    INS.VPCMPEQQ: (64, lambda a, b, w: (1 << w) - 1 if a == b else 0),
    INS.VPCMPGTB: (8, lambda a, b, w: (1 << w) - 1 if _signed(a, w) > _signed(b, w) else 0),
    INS.VPCMPGTW: (16, lambda a, b, w: (1 << w) - 1 if _signed(a, w) > _signed(b, w) else 0),
    INS.VPCMPGTD: (32, lambda a, b, w: (1 << w) - 1 if _signed(a, w) > _signed(b, w) else 0),
    INS.VPCMPGTQ: (64, lambda a, b, w: (1 << w) - 1 if _signed(a, w) > _signed(b, w) else 0),
    INS.VPMINUB: (8, lambda a, b, w: min(a, b)),
    INS.VPMINUW: (16, lambda a, b, w: min(a, b)),
    INS.VPMINUD: (32, lambda a, b, w: min(a, b)),
    INS.VPMAXUB: (8, lambda a, b, w: max(a, b)),
    INS.VPMAXUW: (16, lambda a, b, w: max(a, b)),
    INS.VPMAXUD: (32, lambda a, b, w: max(a, b)),
    INS.VPMINSB: (8, lambda a, b, w: a if _signed(a, w) < _signed(b, w) else b),
    INS.VPMAXSB: (8, lambda a, b, w: a if _signed(a, w) > _signed(b, w) else b),
    INS.VPADDB: (8, lambda a, b, w: a + b),
    INS.VPADDW: (16, lambda a, b, w: a + b),
    INS.VPADDD: (32, lambda a, b, w: a + b),
    INS.VPADDQ: (64, lambda a, b, w: a + b),
    INS.VPSUBB: (8, lambda a, b, w: a - b),
    INS.VPSUBW: (16, lambda a, b, w: a - b),
    INS.VPSUBD: (32, lambda a, b, w: a - b),
    INS.VPSUBQ: (64, lambda a, b, w: a - b),
    INS.VPSUBUSB: (8, lambda a, b, w: max(a - b, 0)),
    INS.VPSUBUSW: (16, lambda a, b, w: max(a - b, 0)),
}

_BROADCAST = {
    INS.VPBROADCASTB: 8,
    INS.VPBROADCASTW: 16,
    INS.VPBROADCASTD: 32,
    INS.VBROADCASTSS: 32,
    INS.VBROADCASTSD: 64,
}


def emulate_avx(dp, instr, op_read, op_write, cip_next) -> bool:
    """Emulate a VEX instruction unicorn rejected. Returns False when it is not one of these."""
    ops = instr.operands

    def is_vector(index: int) -> bool:
        op = ops[index]
        return op.type == CS_OP_REG and instr.reg_name(op.value.reg).startswith(("xmm", "ymm"))

    def write_vector(index: int, value: int):
        # A VEX instruction writing an xmm register clears bits 128-255 of the ymm register
        op = ops[index]
        if op.type == CS_OP_REG:
            name = instr.reg_name(op.value.reg)
            if name.startswith("xmm"):
                dp.regs["ymm" + name[3:]] = value & _MASK128
                return
            dp.regs[name] = value & _MASK256
            return
        op_write(index, value & ((1 << (op.size * 8)) - 1))

    def size(index: int) -> int:
        return ops[index].size

    iid = instr.id
    if iid in _LOGIC and len(ops) == 3:
        bits = size(0) * 8
        write_vector(0, _LOGIC[iid](op_read(1), op_read(2)) & ((1 << bits) - 1))
    elif iid in _LANEWISE and len(ops) == 3:
        width, operation = _LANEWISE[iid]
        count = size(0) * 8 // width
        a = _lanes(op_read(1), width, count)
        b = _lanes(op_read(2), width, count)
        write_vector(0, _join([operation(x, y, width) for x, y in zip(a, b)], width))
    elif iid == INS.VPMOVMSKB:
        count = size(1)
        value = op_read(1)
        mask = 0
        for i in range(count):
            mask |= ((value >> (i * 8 + 7)) & 1) << i
        op_write(0, mask)
    elif iid in (INS.VMOVMSKPS, INS.VMOVMSKPD):
        width = 32 if iid == INS.VMOVMSKPS else 64
        count = size(1) * 8 // width
        value = op_read(1)
        op_write(0, _join([(value >> (i * width + width - 1)) & 1 for i in range(count)], 1))
    elif iid == INS.VPTEST:
        a, b = op_read(0), op_read(1)
        bits = size(0) * 8
        flags = dp.regs.eflags & ~(0x1 | 0x4 | 0x10 | 0x40 | 0x80 | 0x800)  # CF PF AF ZF SF OF
        if a & b == 0:
            flags |= 0x40
        if (~a & ((1 << bits) - 1)) & b == 0:
            flags |= 0x1
        dp.regs.eflags = flags
    elif iid == INS.VZEROUPPER:
        for i in range(16 if dp._x64 else 8):
            dp.regs[f"ymm{i}"] = dp.regs[f"ymm{i}"] & _MASK128
    elif iid == INS.VZEROALL:
        for i in range(16 if dp._x64 else 8):
            dp.regs[f"ymm{i}"] = 0
    elif iid in _BROADCAST:
        width = _BROADCAST[iid]
        value = op_read(1) & ((1 << width) - 1)
        write_vector(0, _join([value] * (size(0) * 8 // width), width))
    elif iid in (INS.VMOVD, INS.VMOVQ):
        bits = 32 if iid == INS.VMOVD else 64
        value = op_read(1) & ((1 << bits) - 1)
        if is_vector(0):
            write_vector(0, value)
        else:
            op_write(0, value)
    elif iid in (INS.VMOVNTDQ, INS.VMOVNTDQA, INS.VMOVUPD, INS.VMOVAPD, INS.VLDDQU):
        value = op_read(1)
        if is_vector(0):
            write_vector(0, value)
        else:
            op_write(0, value)
    elif iid in (INS.VEXTRACTI128, INS.VEXTRACTF128):
        value = op_read(1)
        if op_read(2) & 1:
            value >>= 128
        if is_vector(0):
            write_vector(0, value & _MASK128)
        else:
            op_write(0, value & _MASK128)
    elif iid == INS.VINSERTI128:
        value = op_read(1)
        low = op_read(2) & _MASK128
        if op_read(3) & 1:
            value = (value & _MASK128) | (low << 128)
        else:
            value = (value & (_MASK128 << 128)) | low
        write_vector(0, value)
    elif iid in (INS.VPERM2I128, INS.VPERM2F128):
        a, b, imm = op_read(1), op_read(2), op_read(3)
        halves = [a & _MASK128, a >> 128, b & _MASK128, b >> 128]
        low = 0 if imm & 0x08 else halves[imm & 3]
        high = 0 if imm & 0x80 else halves[(imm >> 4) & 3]
        write_vector(0, low | (high << 128))
    elif iid == INS.VPERMQ:
        source = _lanes(op_read(1), 64, 4)
        imm = op_read(2)
        write_vector(0, _join([source[(imm >> (2 * i)) & 3] for i in range(4)], 64))
    elif iid == INS.VPSHUFB:
        count = size(0)
        a = _lanes(op_read(1), 8, count)
        control = _lanes(op_read(2), 8, count)
        result = []
        for i, c in enumerate(control):
            lane = i // 16 * 16
            result.append(0 if c & 0x80 else a[lane + (c & 0x0F)])
        write_vector(0, _join(result, 8))
    elif iid == INS.VPALIGNR:
        count = size(0)
        a, b, shift = op_read(1), op_read(2), op_read(3)
        result = 0
        for lane in range(count // 16):
            joined = (((a >> (lane * 128)) & _MASK128) << 128) | ((b >> (lane * 128)) & _MASK128)
            result |= ((joined >> (shift * 8)) & _MASK128) << (lane * 128)
        write_vector(0, result)
    elif iid in (INS.VPSLLDQ, INS.VPSRLDQ):
        count = size(0)
        value, shift = op_read(1), min(op_read(2), 16)
        result = 0
        for lane in range(count // 16):
            part = (value >> (lane * 128)) & _MASK128
            part = (part << (shift * 8)) if iid == INS.VPSLLDQ else (part >> (shift * 8))
            result |= (part & _MASK128) << (lane * 128)
        write_vector(0, result)
    elif iid in (INS.VPUNPCKLBW, INS.VPUNPCKHBW):
        count = size(0)
        a = _lanes(op_read(1), 8, count)
        b = _lanes(op_read(2), 8, count)
        result = []
        for lane in range(count // 16):
            start = lane * 16 + (8 if iid == INS.VPUNPCKHBW else 0)
            for i in range(8):
                result += [a[start + i], b[start + i]]
        write_vector(0, _join(result, 8))
    else:
        return False
    cip_next()
    return True
