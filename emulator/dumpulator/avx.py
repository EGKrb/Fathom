# Fathom addition to dumpulator: AVX / AVX2 instructions in Python (and MONITOR / MWAIT). Boost Software License 1.0,
# like the rest of dumpulator (LICENSE.dumpulator).
#
# Unicorn (QEMU 5) has no AVX. A process dumped on a CPU with AVX2 has its C runtime already set up
# for it (ntdll, ucrtbase and vcruntime choose their string and memory routines from CPUID when the
# process starts), so emulated code reaches VEX instructions in wcslen, memcpy, memchr... These are
# emulated here when unicorn reports them as invalid: the integer and logical forms those routines
# use, on 128-bit (xmm) and 256-bit (ymm) registers.

import re
import struct
from fractions import Fraction

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


# the fused multiply-adds (FMA3): vf[n]m{add,sub}{132,213,231}{ps,pd,ss,sd}
_FMA = re.compile(r"v(fn?m)(add|sub)(132|213|231)(ps|pd|ss|sd)$")


_FORMATS = {32: (24, 8, 127), 64: (53, 11, 1023)}  # width: (precision, exponent bits, bias)


def _float_parts(bits: int, width: int):
    """(kind, sign, value) of an IEEE float: kind "nan", "inf" or "num" (value a Fraction)."""
    precision, exponent_bits, bias = _FORMATS[width]
    fraction_bits = precision - 1
    sign = bits >> (width - 1) & 1
    exponent = bits >> fraction_bits & ((1 << exponent_bits) - 1)
    fraction = bits & ((1 << fraction_bits) - 1)
    if exponent == (1 << exponent_bits) - 1:
        return ("nan" if fraction else "inf"), sign, None
    if exponent == 0:
        value = Fraction(fraction, 1 << (fraction_bits + bias - 1))
    else:
        value = Fraction(fraction | (1 << fraction_bits)) * Fraction(2) ** (exponent - bias - fraction_bits)
    return "num", sign, value


def _round_float(sign: int, value: Fraction, width: int) -> int:
    """The float nearest to (-1)^sign * value (> 0), ties to even, with subnormals and overflow to infinity."""
    precision, exponent_bits, bias = _FORMATS[width]
    fraction_bits = precision - 1
    e_min, e_max = 1 - bias, bias
    num, den = value.numerator, value.denominator
    e = num.bit_length() - den.bit_length()
    # 2^e <= value < 2^(e + 1)
    if (num << max(0, -e)) < (den << max(0, e)):
        e -= 1
    e = max(e, e_min)
    shift = e - fraction_bits  # the exponent of the last bit kept
    top, bottom = (num << -shift, den) if shift < 0 else (num, den << shift)
    n, rest = divmod(top, bottom)
    if 2 * rest > bottom or (2 * rest == bottom and n & 1):
        n += 1
    if n >> precision:  # the rounding carried into a new bit
        n >>= 1
        shift += 1
    sign_bit = sign << (width - 1)
    if n >> fraction_bits == 0:  # subnormal (or zero)
        return sign_bit | n
    exponent = shift + fraction_bits
    if exponent > e_max:
        return sign_bit | (((1 << exponent_bits) - 1) << fraction_bits)
    return sign_bit | ((exponent + bias) << fraction_bits) | (n - (1 << fraction_bits))


def _fma_lane(a: int, b: int, c: int, negate: bool, subtract: bool, width: int) -> int:
    """(-)(a * b) (+/-) c rounded once, as the FMA3 instructions compute it (round to nearest)."""
    quiet = 1 << ((22 if width == 32 else 51))
    default_nan = ((1 << (9 if width == 32 else 12)) - 1) << (width - (9 if width == 32 else 12)) | quiet
    parts = [_float_parts(v, width) for v in (a, b, c)]
    for value, (kind, _, _) in zip((a, b, c), parts):
        if kind == "nan":  # the first NaN of a, b, c, quieted
            return value | quiet
    (ka, sa, va), (kb, sb, vb), (kc, sc, vc) = parts
    product_sign = sa ^ sb ^ int(negate)
    addend_sign = sc ^ int(subtract)
    if ka == "inf" or kb == "inf":
        if (ka == "num" and va == 0) or (kb == "num" and vb == 0):
            return default_nan  # infinity times zero
        if kc == "inf" and addend_sign != product_sign:
            return default_nan  # infinity minus infinity
        return _round_float(product_sign, Fraction(2) ** 2000, width)  # an infinity
    if kc == "inf":
        return _round_float(addend_sign, Fraction(2) ** 2000, width)
    product = va * vb
    total = (-product if product_sign else product) + (-vc if addend_sign else vc)
    if total == 0:
        if product == 0 and vc == 0:
            # +0 + +0 = +0, -0 + -0 = -0, otherwise +0 (round to nearest)
            return (product_sign & addend_sign) << (width - 1)
        return 0
    return _round_float(1 if total < 0 else 0, abs(total), width)


def _fused(match, d: int, s2: int, s3: int, size: int) -> int:
    negate, subtract = match.group(1) == "fnm", match.group(2) == "sub"
    order, kind = match.group(3), match.group(4)
    width = 32 if kind in ("ps", "ss") else 64
    count = 1 if kind in ("ss", "sd") else size * 8 // width
    mask = (1 << width) - 1
    lanes = []
    for i in range(count):
        x, y, z = ((v >> (i * width)) & mask for v in (d, s2, s3))
        a, b, c = {"132": (x, z, y), "213": (y, x, z), "231": (y, z, x)}[order]
        lanes.append(_fma_lane(a, b, c, negate, subtract, width))
    out = _join(lanes, width)
    if kind in ("ss", "sd"):  # the other lanes of the destination stay
        out |= d & _MASK128 & ~mask
    return out


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
    if iid in (INS.MONITOR, INS.MWAIT, INS.MONITORX, INS.MWAITX):
        # wait for a write to an address (spin loops of critical sections, chosen at start-up on
        # the CPU the process ran on): a wait that ends at once, which these instructions allow
        pass
    elif iid in _LOGIC and len(ops) == 3:
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
    elif _FMA.match(instr.mnemonic):
        write_vector(0, _fused(_FMA.match(instr.mnemonic), op_read(0), op_read(1), op_read(2), size(0)))
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
