"""Check the AVX / AVX2 / FMA3 instructions dumpulator emulates in Python (dumpulator/avx.py) against the CPU.

Each instruction is encoded (VEX), run natively on random registers, then emulated on the same
registers; the destination registers, the general register and the flags must match. Needs a 64-bit
Windows on a CPU with AVX2 (and FMA3, which comes with it; the test is skipped otherwise).

    python emulator/tests/test_avx.py [--rounds 200]
"""
from __future__ import annotations

import argparse
import ctypes
import os
import random
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import capstone  # noqa: E402
from capstone import CS_OP_IMM, CS_OP_REG  # noqa: E402

from dumpulator.avx import emulate_avx  # noqa: E402

# name: (map, pp, W, opcode, form, L values), map 1 = 0F, 2 = 0F38, 3 = 0F3A; pp 0 = none, 1 = 66
# forms: "rvm" dst, src1 (vvvv), src2 (rm) / "rm" dst, src (rm) / "gpr" r32, ymm (rm) /
#        "test" src1 (reg), src2 (rm) / "rmi" dst, src, imm / "rvmi" dst, src1, src2, imm /
#        "mr i" rm = dst, reg = src, imm (vextracti128) / "shift" vvvv = dst, rm = src, reg = /n, imm
INSTRUCTIONS = {
    "vpxor": (1, 1, 0, 0xEF, "rvm", (0, 1)),
    "vpor": (1, 1, 0, 0xEB, "rvm", (0, 1)),
    "vpand": (1, 1, 0, 0xDB, "rvm", (0, 1)),
    "vpandn": (1, 1, 0, 0xDF, "rvm", (0, 1)),
    "vxorps": (1, 0, 0, 0x57, "rvm", (0, 1)),
    "vorps": (1, 0, 0, 0x56, "rvm", (0, 1)),
    "vandps": (1, 0, 0, 0x54, "rvm", (0, 1)),
    "vandnps": (1, 0, 0, 0x55, "rvm", (0, 1)),
    "vpcmpeqb": (1, 1, 0, 0x74, "rvm", (0, 1)),
    "vpcmpeqw": (1, 1, 0, 0x75, "rvm", (0, 1)),
    "vpcmpeqd": (1, 1, 0, 0x76, "rvm", (0, 1)),
    "vpcmpeqq": (2, 1, 0, 0x29, "rvm", (0, 1)),
    "vpcmpgtb": (1, 1, 0, 0x64, "rvm", (0, 1)),
    "vpcmpgtw": (1, 1, 0, 0x65, "rvm", (0, 1)),
    "vpcmpgtd": (1, 1, 0, 0x66, "rvm", (0, 1)),
    "vpcmpgtq": (2, 1, 0, 0x37, "rvm", (0, 1)),
    "vpminub": (1, 1, 0, 0xDA, "rvm", (0, 1)),
    "vpminuw": (2, 1, 0, 0x3A, "rvm", (0, 1)),
    "vpminud": (2, 1, 0, 0x3B, "rvm", (0, 1)),
    "vpmaxub": (1, 1, 0, 0xDE, "rvm", (0, 1)),
    "vpmaxuw": (2, 1, 0, 0x3E, "rvm", (0, 1)),
    "vpmaxud": (2, 1, 0, 0x3F, "rvm", (0, 1)),
    "vpminsb": (2, 1, 0, 0x38, "rvm", (0, 1)),
    "vpmaxsb": (2, 1, 0, 0x3C, "rvm", (0, 1)),
    "vpaddb": (1, 1, 0, 0xFC, "rvm", (0, 1)),
    "vpaddw": (1, 1, 0, 0xFD, "rvm", (0, 1)),
    "vpaddd": (1, 1, 0, 0xFE, "rvm", (0, 1)),
    "vpaddq": (1, 1, 0, 0xD4, "rvm", (0, 1)),
    "vpsubb": (1, 1, 0, 0xF8, "rvm", (0, 1)),
    "vpsubw": (1, 1, 0, 0xF9, "rvm", (0, 1)),
    "vpsubd": (1, 1, 0, 0xFA, "rvm", (0, 1)),
    "vpsubq": (1, 1, 0, 0xFB, "rvm", (0, 1)),
    "vpsubusb": (1, 1, 0, 0xD8, "rvm", (0, 1)),
    "vpsubusw": (1, 1, 0, 0xD9, "rvm", (0, 1)),
    "vpshufb": (2, 1, 0, 0x00, "rvm", (0, 1)),
    "vpunpcklbw": (1, 1, 0, 0x60, "rvm", (0, 1)),
    "vpunpckhbw": (1, 1, 0, 0x68, "rvm", (0, 1)),
    "vpmovmskb": (1, 1, 0, 0xD7, "gpr", (0, 1)),
    "vmovmskps": (1, 0, 0, 0x50, "gpr", (0, 1)),
    "vptest": (2, 1, 0, 0x17, "test", (0, 1)),
    "vpbroadcastb": (2, 1, 0, 0x78, "rm", (0, 1)),
    "vpbroadcastw": (2, 1, 0, 0x79, "rm", (0, 1)),
    "vpbroadcastd": (2, 1, 0, 0x58, "rm", (0, 1)),
    "vpalignr": (3, 1, 0, 0x0F, "rvmi", (0, 1)),
    "vpermq": (3, 1, 1, 0x00, "rmi", (1,)),
    "vperm2i128": (3, 1, 0, 0x46, "rvmi", (1,)),
    "vinserti128": (3, 1, 0, 0x38, "rvmi1", (1,)),
    "vextracti128": (3, 1, 0, 0x39, "mri", (1,)),
    "vpslldq": (1, 1, 0, 0x73, "shift7", (0, 1)),
    "vpsrldq": (1, 1, 0, 0x73, "shift3", (0, 1)),
}
# the fused multiply-adds (FMA3): 0F38, 66, W = 0 for single and 1 for double precision
for _op, _base in (("fmadd", 0x98), ("fmsub", 0x9A), ("fnmadd", 0x9C), ("fnmsub", 0x9E)):
    for _order, _offset in (("132", 0x00), ("213", 0x10), ("231", 0x20)):
        for _kind, _w, _scalar in (("ps", 0, 0), ("pd", 1, 0), ("ss", 0, 1), ("sd", 1, 1)):
            INSTRUCTIONS[f"v{_op}{_order}{_kind}"] = (2, 1, _w, _base + _offset + _scalar, "rvm", (0,) if _scalar else (0, 1))

DST, SRC1, SRC2 = 0, 1, 2  # ymm0 = destination, ymm1, ymm2 = sources


def special_floats(rng: random.Random, width: int) -> int:
    """256 bits of floats of a width, often special: NaNs (quiet, signaling, both signs), infinities,
    zeros, subnormals, the largest, values that overflow or cancel."""
    exponent_bits, fraction_bits = (8, 23) if width == 32 else (11, 52)
    top = (1 << exponent_bits) - 1
    value = 0
    for i in range(256 // width):
        sign = rng.getrandbits(1) << (width - 1)
        choice = rng.randrange(10)
        if choice == 0:
            lane = (top << fraction_bits) | rng.getrandbits(fraction_bits) | 1   # NaN
        elif choice == 1:
            lane = top << fraction_bits                                           # infinity
        elif choice == 2:
            lane = 0                                                              # zero
        elif choice == 3:
            lane = rng.getrandbits(fraction_bits)                                 # subnormal
        elif choice == 4:
            lane = ((top - 1) << fraction_bits) | rng.getrandbits(fraction_bits)  # huge
        elif choice == 5:
            lane = (rng.randrange(1, 4) << fraction_bits) | rng.getrandbits(fraction_bits)  # tiny
        else:
            bias = top >> 1
            lane = (rng.randrange(bias - 30, bias + 30) << fraction_bits) | rng.getrandbits(fraction_bits)
        value |= (sign | lane) << (i * width)
    return value


def vex(mmmmm: int, pp: int, w: int, length: int, vvvv: int, opcode: int, reg: int, rm: int) -> bytes:
    # 3-byte VEX, registers 0-7 only (R, X, B = 1: not extended)
    byte1 = 0xE0 | mmmmm
    byte2 = (w << 7) | ((~vvvv & 0xF) << 3) | (length << 2) | pp
    modrm = 0xC0 | (reg << 3) | rm
    return bytes((0xC4, byte1, byte2, opcode, modrm))


def encode(name: str, length: int, imm: int) -> bytes:
    mmmmm, pp, w, opcode, form, _ = INSTRUCTIONS[name]
    if form == "rvm":
        return vex(mmmmm, pp, w, length, SRC1, opcode, DST, SRC2)
    if form == "rm":
        return vex(mmmmm, pp, w, length, 0, opcode, DST, SRC1)
    if form == "gpr":  # eax = mask of ymm1
        return vex(mmmmm, pp, w, length, 0, opcode, 0, SRC1)
    if form == "test":
        return vex(mmmmm, pp, w, length, 0, opcode, SRC1, SRC2)
    if form == "rmi":
        return vex(mmmmm, pp, w, length, 0, opcode, DST, SRC1) + bytes((imm,))
    if form in ("rvmi", "rvmi1"):
        return vex(mmmmm, pp, w, length, SRC1, opcode, DST, SRC2) + bytes((imm,))
    if form == "mri":  # vextracti128 xmm0, ymm1, imm
        return vex(mmmmm, pp, w, length, 0, opcode, SRC1, DST) + bytes((imm,))
    if form.startswith("shift"):  # vpslldq ymm0, ymm1, imm: vvvv = destination, rm = source
        return vex(mmmmm, pp, w, length, DST, opcode, int(form[5:]), SRC1) + bytes((imm,))
    raise ValueError(form)


# Native runner: rcx = buffer. Loads ymm0-ymm2 from [rcx], eax from [rcx+96] and the flags from
# [rcx+104], runs the instruction, stores ymm0-ymm2, eax and the flags at [rcx+128...]
def native_code(instruction: bytes) -> bytes:
    code = bytearray()
    for i in range(3):  # vmovdqu ymm_i, [rcx + 32 i]
        code += bytes((0xC5, 0xFE, 0x6F, 0x41 | (i << 3), 32 * i))
    code += bytes((0x8B, 0x41, 96))  # mov eax, [rcx+96]
    code += bytes((0xFF, 0x71, 104, 0x9D))  # push [rcx+104] ; popfq
    code += instruction
    code += bytes((0x9C, 0x8F, 0x81)) + struct.pack("<I", 232)  # pushfq ; pop [rcx+232]
    for i in range(3):  # vmovdqu [rcx + 128 + 32 i], ymm_i
        code += bytes((0xC5, 0xFE, 0x7F, 0x81 | (i << 3))) + struct.pack("<I", 128 + 32 * i)
    code += bytes((0x89, 0x81)) + struct.pack("<I", 224)  # mov [rcx+224], eax
    code += bytes((0xC5, 0xF8, 0x77, 0xC3))  # vzeroupper ; ret
    return bytes(code)


class Native:
    def __init__(self):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.VirtualAlloc.restype = ctypes.c_void_p
        kernel32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32]
        self.code = kernel32.VirtualAlloc(None, 0x1000, 0x3000, 0x40)
        self.buffer = ctypes.create_string_buffer(256)

    def run(self, instruction: bytes, regs: list[int], eax: int, flags: int):
        code = native_code(instruction)
        ctypes.memmove(self.code, code, len(code))
        data = b"".join(r.to_bytes(32, "little") for r in regs) + struct.pack("<IIQ", eax, 0, flags)
        ctypes.memmove(self.buffer, data, len(data))
        ctypes.CFUNCTYPE(None, ctypes.c_void_p)(self.code)(ctypes.addressof(self.buffer))
        out = self.buffer.raw
        ymm = [int.from_bytes(out[128 + 32 * i:160 + 32 * i], "little") for i in range(3)]
        return ymm, struct.unpack_from("<I", out, 224)[0], struct.unpack_from("<Q", out, 232)[0]


class FakeRegs:
    def __init__(self, ymm, eax, flags):
        self.values = {f"ymm{i}": v for i, v in enumerate(ymm)}
        self.values["rax"] = eax
        self.values["eflags"] = flags

    def __getitem__(self, name):
        if name.startswith("xmm"):
            return self.values["ymm" + name[3:]] & ((1 << 128) - 1)
        if name in ("eax", "rax"):
            return self.values["rax"]
        return self.values[name]

    def __setitem__(self, name, value):
        if name.startswith("xmm"):  # a legacy write keeps the upper half
            full = self.values["ymm" + name[3:]]
            self.values["ymm" + name[3:]] = (full & ~((1 << 128) - 1)) | (value & ((1 << 128) - 1))
        elif name in ("eax", "rax"):
            self.values["rax"] = value & 0xFFFFFFFF
        else:
            self.values[name] = value

    def __getattr__(self, name):
        return self.values[name] if name in ("eflags",) else object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        if name == "eflags":
            self.values["eflags"] = value
        else:
            object.__setattr__(self, name, value)


class FakeDumpulator:
    _x64 = True

    def __init__(self, regs):
        self.regs = regs


def emulate(instruction: bytes, regs: list[int], eax: int, flags: int):
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    instr = next(md.disasm(instruction, 0x1000))
    fake = FakeRegs(regs, eax, flags)
    dp = FakeDumpulator(fake)

    def op_read(index):
        op = instr.operands[index]
        if op.type == CS_OP_REG:
            return fake[instr.reg_name(op.value.reg)]
        if op.type == CS_OP_IMM:
            return op.value.imm & 0xFF
        raise NotImplementedError("memory operand")

    def op_write(index, value):
        op = instr.operands[index]
        fake[instr.reg_name(op.value.reg)] = value

    moved = []
    ok = emulate_avx(dp, instr, op_read, op_write, lambda: moved.append(instr.size))
    if not ok:
        raise AssertionError(f"not emulated: {instr.mnemonic} {instr.op_str}")
    assert moved == [instr.size]
    return [fake.values[f"ymm{i}"] for i in range(3)], fake.values["rax"] & 0xFFFFFFFF, fake.values["eflags"], instr


def cpu_has_avx2() -> bool:
    if os.name != "nt" or struct.calcsize("P") != 8:
        return False
    PF_AVX2_INSTRUCTIONS_AVAILABLE = 40
    return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(PF_AVX2_INSTRUCTIONS_AVAILABLE))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=200)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    if not cpu_has_avx2():
        print("SKIP: needs 64-bit Windows on a CPU with AVX2")
        return 0
    rng = random.Random(args.seed)
    native = Native()
    failures = 0
    checked = 0
    flag_mask = 0x8D5  # CF PF AF ZF SF OF
    for name, (_, _, _, _, form, lengths) in INSTRUCTIONS.items():
        for length in lengths:
            for round_ in range(args.rounds):
                regs = [rng.getrandbits(256) for _ in range(3)]
                if name.startswith("vf") and round_ % 2 == 0:
                    width = 32 if name.endswith(("ps", "ss")) else 64
                    regs = [special_floats(rng, width) for _ in range(3)]
                if round_ % 4 == 1:  # equal lanes and zeros, for the compares, vptest and the minimum
                    regs[2] = regs[1] if round_ % 8 == 1 else 0
                if round_ % 4 == 2:
                    regs[1] = regs[2] & rng.getrandbits(256)
                imm = rng.getrandbits(8)
                if form == "shift3" or form == "shift7":
                    imm &= 0x1F
                eax = rng.getrandbits(32)
                flags = 0x202 | (rng.getrandbits(12) & flag_mask)
                instruction = encode(name, length, imm)
                want_ymm, want_eax, want_flags = native.run(instruction, regs, eax, flags)
                got_ymm, got_eax, got_flags, instr = emulate(instruction, regs, eax, flags)
                problems = []
                if got_ymm != want_ymm:
                    for i in range(3):
                        if got_ymm[i] != want_ymm[i]:
                            problems.append(f"ymm{i}: emulated {got_ymm[i]:064x}, cpu {want_ymm[i]:064x}")
                if got_eax != want_eax:
                    problems.append(f"eax: emulated {got_eax:#x}, cpu {want_eax:#x}")
                if (got_flags & flag_mask) != (want_flags & flag_mask):
                    problems.append(f"flags: emulated {got_flags & flag_mask:#x}, cpu {want_flags & flag_mask:#x}")
                checked += 1
                if problems:
                    failures += 1
                    if failures <= 10:
                        print(f"FAIL {instr.mnemonic} {instr.op_str} ({instruction.hex()})")
                        for problem in problems:
                            print("   ", problem)
                    break
    print(f"{checked} executions of {len(INSTRUCTIONS)} instructions, {failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
