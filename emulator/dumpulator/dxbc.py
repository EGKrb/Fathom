# Fathom addition to dumpulator: an interpreter of the shaders of Direct3D 10 / 11 (DXBC, shader models
# 4.0 to 5.0) for the Direct3D 11 of d3d_emu.py. Boost Software License 1.0, like the rest of
# dumpulator (LICENSE.dumpulator).
#
# The shader runs on all its lanes at once (every vertex of a draw, every pixel of a triangle): a
# register is an (n, 4) array of 32-bit values, an instruction a numpy operation, the control flow
# (if / else, loop / break / continue, ret, discard) masks of active lanes. Covered: the arithmetic
# (float, integer, conversions, comparisons, movc), constant buffers and the immediate constant
# buffer, indexable temporaries (dynamic indices), sample / sample_l / sample_b / sample_c_lz / ld /
# resinfo on 2D textures. Not covered (Unsupported, the draw is skipped and named): geometry, hull,
# domain and compute shaders, UAVs, subroutines (call, interfaces), switch, gathers; the derivatives
# (deriv_rtx / rty) give 0.

import struct
from typing import Dict, List, Optional

import numpy


class Unsupported(Exception):
    pass


# the opcodes (d3d10tokenizedprogramformat.hpp / d3d11tokenizedprogramformat.hpp)
OPS = dict(ADD=0, AND=1, BREAK=2, BREAKC=3, CALL=4, CALLC=5, CASE=6, CONTINUE=7, CONTINUEC=8, CUT=9, DEFAULT=10,
           DERIV_RTX=11, DERIV_RTY=12, DISCARD=13, DIV=14, DP2=15, DP3=16, DP4=17, ELSE=18, EMIT=19, EMITTHENCUT=20,
           ENDIF=21, ENDLOOP=22, ENDSWITCH=23, EQ=24, EXP=25, FRC=26, FTOI=27, FTOU=28, GE=29, IADD=30, IF=31, IEQ=32,
           IGE=33, ILT=34, IMAD=35, IMAX=36, IMIN=37, IMUL=38, INE=39, INEG=40, ISHL=41, ISHR=42, ITOF=43, LABEL=44, LD=45,
           LD_MS=46, LOG=47, LOOP=48, LT=49, MAD=50, MIN=51, MAX=52, CUSTOMDATA=53, MOV=54, MOVC=55, MUL=56, NE=57, NOP=58,
           NOT=59, OR=60, RESINFO=61, RET=62, RETC=63, ROUND_NE=64, ROUND_NI=65, ROUND_PI=66, ROUND_Z=67, RSQ=68,
           SAMPLE=69, SAMPLE_C=70, SAMPLE_C_LZ=71, SAMPLE_L=72, SAMPLE_D=73, SAMPLE_B=74, SQRT=75, SWITCH=76, SINCOS=77,
           UDIV=78, ULT=79, UGE=80, UMUL=81, UMAD=82, UMAX=83, UMIN=84, USHR=85, UTOF=86, XOR=87, LOD=108,
           DERIV_RTX_COARSE=122, DERIV_RTX_FINE=123, DERIV_RTY_COARSE=124, DERIV_RTY_FINE=125, RCP=129, F32TOF16=130,
           F16TOF32=131, COUNTBITS=134, UBFE=138, IBFE=139, BFI=140, BFREV=141, SWAPC=142)
NAMES = {v: k for k, v in OPS.items()}
# declarations: dcl_* of 4.x (88..106), of 5.0 (143..162, 0xCE...)
DECLARATIONS = set(range(88, 107)) | set(range(143, 163)) | {113, 114, 115, 116, 0xCD, 0xCE}
DCL_CONSTANT_BUFFER, DCL_TEMPS, DCL_INDEXABLE_TEMP, DCL_INPUT_PS, DCL_INPUT_PS_SIV, DCL_INPUT_PS_SGV = 89, 104, 105, 98, 100, 99
TEMP, INPUT, OUTPUT, INDEXABLE_TEMP, IMM32, IMM64, SAMPLER, RESOURCE, CONSTANT_BUFFER, ICB, LABEL, PRIMITIVE_ID, \
    OUTPUT_DEPTH, NULL = range(14)
OUTPUT_DEPTH_GE, OUTPUT_DEPTH_LE = 0x26, 0x27
MAX_LOOP = 4096


class Operand:
    __slots__ = ("type", "mask", "swizzle", "modifier", "indices", "imm")

    def __repr__(self):
        return f"Operand(type={self.type}, indices={self.indices}, mask={self.mask:x}, swizzle={self.swizzle})"


class Instruction:
    __slots__ = ("opcode", "saturate", "nonzero", "control", "operands", "jump")

    def __init__(self, opcode, saturate, nonzero, control, operands):
        self.opcode, self.saturate, self.nonzero, self.control = opcode, saturate, nonzero, control
        self.operands, self.jump = operands, None


def _operand(tokens, i):
    token = tokens[i]
    i += 1
    op = Operand()
    components = token & 3
    op.type = (token >> 12) & 0xFF
    dimension = (token >> 20) & 3
    op.mask, op.swizzle, op.modifier, op.imm = 0xF, (0, 1, 2, 3), 0, None
    if components == 2:
        mode = (token >> 2) & 3
        if mode == 0:
            op.mask = (token >> 4) & 0xF
        elif mode == 1:
            op.swizzle = tuple((token >> (4 + 2 * k)) & 3 for k in range(4))
        else:
            op.swizzle = ((token >> 4) & 3,) * 4
    elif components == 1:
        op.mask, op.swizzle = 1, (0, 0, 0, 0)
    elif components == 0:
        op.mask = 0
    extended = token >> 31
    while extended:
        ext = tokens[i]
        i += 1
        if ext & 0x3F == 1:
            op.modifier = (ext >> 6) & 0xFF
        extended = ext >> 31
    if op.type == IMM32:
        count = 4 if components == 2 else 1
        op.imm = numpy.array(list(tokens[i:i + count]) * (4 // count), dtype=numpy.uint32)
        i += count
    elif op.type == IMM64:
        raise Unsupported("64-bit immediates")
    op.indices = []
    for d in range(dimension):
        representation = (token >> (22 + 3 * d)) & 7
        if representation == 0:
            op.indices.append((tokens[i], None))
            i += 1
        elif representation == 1:
            op.indices.append((tokens[i + 1], None))
            i += 2
        elif representation == 2:
            rel, i = _operand(tokens, i)
            op.indices.append((0, rel))
        elif representation == 3:
            base = tokens[i]
            rel, i = _operand(tokens, i + 1)
            op.indices.append((base, rel))
        else:
            raise Unsupported("64-bit relative indices")
    return op, i


def chunks(data: bytes) -> Dict[bytes, bytes]:
    if data[:4] != b"DXBC":
        raise Unsupported("not a DXBC shader")
    count = struct.unpack_from("<I", data, 28)[0]
    out = {}
    for k in range(count):
        offset = struct.unpack_from("<I", data, 32 + 4 * k)[0]
        size = struct.unpack_from("<I", data, offset + 4)[0]
        out[data[offset:offset + 4]] = data[offset + 8:offset + 8 + size]
    return out


def signature(chunk: Optional[bytes], tag: bytes) -> List[dict]:
    if not chunk:
        return []
    count = struct.unpack_from("<I", chunk, 0)[0]
    stride, base = {b"ISG1": (32, 4), b"OSG1": (32, 4), b"OSG5": (28, 4)}.get(tag, (24, 0))
    out = []
    for k in range(count):
        offset = 8 + k * stride + base
        name, index, sv, kind, register = struct.unpack_from("<5I", chunk, offset)
        mask = chunk[offset + 20]
        end = chunk.index(b"\0", name)
        out.append(dict(name=chunk[name:end].decode("latin-1").upper(), index=index, sv=sv, kind=kind, register=register,
                        mask=mask))
    return out


class Shader:
    """A DXBC shader: kind (0 pixel, 1 vertex, 2 geometry...), inputs / outputs (signatures), program."""

    def __init__(self, bytecode: bytes):
        parts = chunks(bytecode)
        code = parts.get(b"SHEX") or parts.get(b"SHDR")
        if code is None:
            raise Unsupported("no shader code")
        for tag in (b"ISGN", b"ISG1"):
            if tag in parts:
                self.inputs = signature(parts[tag], tag)
                break
        else:
            self.inputs = []
        for tag in (b"OSGN", b"OSG1", b"OSG5"):
            if tag in parts:
                self.outputs = signature(parts[tag], tag)
                break
        else:
            self.outputs = []
        version, length = struct.unpack_from("<2I", code, 0)
        self.kind = version >> 16
        self.model = ((version >> 4) & 0xF, version & 0xF)
        tokens = struct.unpack_from(f"<{length}I", code, 0)
        self.temps = 0
        self.indexable = {}
        self.icb = None
        self.interpolation = {}
        self.program: List[Instruction] = []
        self.error = None
        i = 2
        try:
            while i < length:
                token = tokens[i]
                opcode = token & 0x7FF
                if opcode == OPS["CUSTOMDATA"]:
                    size = tokens[i + 1]
                    if (token >> 11) == 3:  # the immediate constant buffer
                        self.icb = numpy.array(tokens[i + 2:i + size], dtype=numpy.uint32).reshape(-1, 4)
                    i += max(2, size)
                    continue
                size = (token >> 24) & 0x7F if opcode in DECLARATIONS else (token >> 24) & 0x1F
                if size == 0:
                    raise Unsupported(f"opcode {opcode} without a length")
                end = i + size
                if opcode in DECLARATIONS:
                    if opcode == DCL_TEMPS:
                        self.temps = tokens[i + 1]
                    elif opcode == DCL_INDEXABLE_TEMP:
                        self.indexable[tokens[i + 1]] = tokens[i + 2]
                    elif opcode in (DCL_INPUT_PS, DCL_INPUT_PS_SIV, DCL_INPUT_PS_SGV):
                        op, _ = _operand(tokens, i + 1)
                        if op.indices:
                            self.interpolation[op.indices[0][0]] = (token >> 11) & 0xF
                else:
                    j = i + 1
                    extended = token >> 31
                    while extended:
                        extended = tokens[j] >> 31
                        j += 1
                    operands = []
                    while j < end:
                        op, j = _operand(tokens, j)
                        operands.append(op)
                    self.program.append(Instruction(opcode, (token >> 13) & 1, (token >> 18) & 1, (token >> 11) & 0x1FFF,
                                                    operands))
                i = end
        except Unsupported as e:
            self.error = str(e)
        self._link()
        unsupported = sorted({NAMES.get(ins.opcode, str(ins.opcode)) for ins in self.program
                              if ins.opcode not in _SUPPORTED})
        if unsupported and self.error is None:
            self.error = "instructions " + ", ".join(unsupported)
        if self.kind not in (0, 1) and self.error is None:
            self.error = {2: "geometry shaders", 3: "hull shaders", 4: "domain shaders", 5: "compute shaders"}.get(
                self.kind, "this kind of shader")

    def _link(self):
        """The matching else / endif / endloop of if / else / loop."""
        stack = []
        for index, ins in enumerate(self.program):
            if ins.opcode in (OPS["IF"], OPS["LOOP"]):
                stack.append(index)
            elif ins.opcode == OPS["ELSE"] and stack:
                self.program[stack[-1]].jump = index
                stack[-1] = index
            elif ins.opcode in (OPS["ENDIF"], OPS["ENDLOOP"]) and stack:
                opening = stack.pop()
                self.program[opening].jump = index
                ins.jump = opening

    def register_of(self, semantic: str, index: int, outputs=False) -> Optional[int]:
        for element in self.outputs if outputs else self.inputs:
            if element["name"] == semantic.upper() and element["index"] == index:
                return element["register"]
        return None

    def run(self, n: int, inputs: Dict[int, numpy.ndarray], constant_buffers=None, textures=None, samplers=None,
            positions=None):
        """Runs the shader on n lanes. inputs: register -> (n, 4) uint32. constant_buffers: slot ->
        (k, 4) uint32. textures: slot -> (h, w, 4) float32. samplers: slot -> (linear, address).
        The result: (outputs register -> (n, 4) uint32, alive mask, depth or None)."""
        if self.error:
            raise Unsupported(self.error)
        return _Machine(self, n, inputs, constant_buffers or {}, textures or {}, samplers or {}).execute()


def _f(a):
    return a.view(numpy.float32)


def _u(a):
    return numpy.ascontiguousarray(a, dtype=numpy.float32).view(numpy.uint32)


class _Machine:
    def __init__(self, shader: Shader, n, inputs, cbs, textures, samplers):
        self.s = shader
        self.n = n
        self.inputs = inputs
        self.cbs = cbs
        self.textures = textures
        self.samplers = samplers
        self.temps: Dict[int, numpy.ndarray] = {}
        self.outputs: Dict[int, numpy.ndarray] = {}
        self.indexable = {k: numpy.zeros((n, size, 4), dtype=numpy.uint32) for k, size in shader.indexable.items()}
        self.depth = None
        self.active = numpy.ones(n, dtype=bool)
        self.dead = numpy.zeros(n, dtype=bool)
        self.discarded = numpy.zeros(n, dtype=bool)

    # ------------------------------------------------------------------ operands
    def _index(self, index):
        base, rel = index
        if rel is None:
            return base
        return base + self.read(rel, "i")[:, 0].astype(numpy.int64)

    def _raw(self, op: Operand) -> numpy.ndarray:
        n = self.n
        t = op.type
        if t == IMM32:
            return numpy.broadcast_to(op.imm, (n, 4))
        if t == TEMP:
            return self.temps.setdefault(op.indices[0][0], numpy.zeros((n, 4), dtype=numpy.uint32))
        if t == INPUT:
            index = op.indices[-1][0] if op.indices[-1][1] is None else None
            if index is None:
                raise Unsupported("relative input indices")
            value = self.inputs.get(index)
            return value if value is not None else numpy.zeros((n, 4), dtype=numpy.uint32)
        if t == OUTPUT:
            return self.outputs.setdefault(op.indices[0][0], numpy.zeros((n, 4), dtype=numpy.uint32))
        if t == INDEXABLE_TEMP:
            array = self.indexable[op.indices[0][0]]
            index = self._index(op.indices[1])
            if isinstance(index, numpy.ndarray):
                return array[numpy.arange(n), numpy.clip(index, 0, array.shape[1] - 1)]
            return array[:, index]
        if t == CONSTANT_BUFFER:
            buffer = self.cbs.get(op.indices[0][0])
            if buffer is None or len(buffer) == 0:
                return numpy.zeros((n, 4), dtype=numpy.uint32)
            index = self._index(op.indices[1])
            if isinstance(index, numpy.ndarray):
                return buffer[numpy.clip(index, 0, len(buffer) - 1)]
            return numpy.broadcast_to(buffer[min(index, len(buffer) - 1)], (n, 4))
        if t == ICB:
            if self.s.icb is None:
                return numpy.zeros((n, 4), dtype=numpy.uint32)
            index = self._index(op.indices[0])
            if isinstance(index, numpy.ndarray):
                return self.s.icb[numpy.clip(index, 0, len(self.s.icb) - 1)]
            return numpy.broadcast_to(self.s.icb[min(index, len(self.s.icb) - 1)], (n, 4))
        if t == PRIMITIVE_ID:
            return numpy.zeros((n, 4), dtype=numpy.uint32)
        raise Unsupported(f"operand type {t}")

    def read(self, op: Operand, kind="f") -> numpy.ndarray:
        """The value of a source operand (swizzled, with its modifier): float32, int32 or uint32."""
        value = self._raw(op)[:, list(op.swizzle)]
        if kind == "f":
            value = value.view(numpy.float32)
            if op.modifier & 2:
                value = numpy.abs(value)
            if op.modifier & 1:
                value = -value
            return value
        value = value.view(numpy.int32) if kind == "i" else value.view(numpy.uint32)
        if op.modifier & 2:
            value = numpy.abs(value.view(numpy.int32)).view(value.dtype)
        if op.modifier & 1:
            value = (-value.view(numpy.int32)).view(value.dtype)
        return value

    def write(self, op: Operand, value: numpy.ndarray, saturate=False):
        if op.type == NULL:
            return
        if value.dtype == numpy.float32 or value.dtype == numpy.float64:
            value = value.astype(numpy.float32)
            if saturate:
                value = numpy.clip(numpy.nan_to_num(value, nan=0.0), 0.0, 1.0)
            value = value.view(numpy.uint32)
        else:
            value = value.astype(numpy.uint32, copy=False) if value.dtype != numpy.int32 else value.view(numpy.uint32)
        if value.ndim == 1:
            value = numpy.repeat(value[:, None], 4, axis=1)
        if op.type in (OUTPUT_DEPTH, OUTPUT_DEPTH_GE, OUTPUT_DEPTH_LE):
            if self.depth is None:
                self.depth = numpy.zeros(self.n, dtype=numpy.float32)
            self.depth[self.active] = value[self.active, 0].view(numpy.float32)
            return
        if op.type == INDEXABLE_TEMP:
            array = self.indexable[op.indices[0][0]]
            index = self._index(op.indices[1])
            lanes = numpy.nonzero(self.active)[0]
            rows = numpy.clip(index[lanes] if isinstance(index, numpy.ndarray) else numpy.full(len(lanes), index), 0,
                              array.shape[1] - 1)
            for c in range(4):
                if op.mask >> c & 1:
                    array[lanes, rows, c] = value[lanes, c]
            return
        target = self._raw(op)
        for c in range(4):
            if op.mask >> c & 1:
                target[self.active, c] = value[self.active, c]

    # ------------------------------------------------------------------ execution
    def execute(self):
        program = self.s.program
        frames = []      # ("if", previous active, condition) / ("loop", previous active, broken, continued, count)
        pc = 0
        with numpy.errstate(all="ignore"):
            while pc < len(program):
                ins = program[pc]
                code = ins.opcode
                if code == OPS["IF"]:
                    condition = self._test(ins)
                    frames.append(["if", self.active.copy(), condition])
                    self.active = self.active & condition
                elif code == OPS["ELSE"]:
                    frame = frames[-1]
                    self.active = frame[1] & ~frame[2] & ~self.dead
                    self.active &= self._loop_alive(frames[:-1])
                elif code == OPS["ENDIF"]:
                    frame = frames.pop()
                    self.active = frame[1] & ~self.dead & self._loop_alive(frames)
                elif code == OPS["LOOP"]:
                    frames.append(["loop", self.active.copy(), numpy.zeros(self.n, bool), numpy.zeros(self.n, bool), 0])
                elif code == OPS["ENDLOOP"]:
                    frame = frames[-1]
                    frame[3][:] = False
                    frame[4] += 1
                    self.active = frame[1] & ~frame[2] & ~self.dead
                    if self.active.any() and frame[4] < MAX_LOOP:
                        pc = ins.jump + 1
                        continue
                    frames.pop()
                    self.active = frame[1] & ~self.dead & self._loop_alive(frames)
                elif code in (OPS["BREAK"], OPS["BREAKC"], OPS["CONTINUE"], OPS["CONTINUEC"]):
                    hit = self.active & (self._test(ins) if code in (OPS["BREAKC"], OPS["CONTINUEC"]) else True)
                    loop = next(f for f in reversed(frames) if f[0] == "loop")
                    loop[2 if code in (OPS["BREAK"], OPS["BREAKC"]) else 3] |= hit
                    self.active = self.active & ~hit
                elif code in (OPS["RET"], OPS["RETC"]):
                    hit = self.active & (self._test(ins) if code == OPS["RETC"] else True)
                    self.dead |= hit
                    self.active = self.active & ~hit
                    if not frames and not self.active.any():
                        break
                elif code == OPS["DISCARD"]:
                    hit = self.active & self._test(ins)
                    self.discarded |= hit
                    self.dead |= hit
                    self.active = self.active & ~hit
                elif self.active.any():
                    self._compute(ins)
                pc += 1
        return self.outputs, ~self.discarded, self.depth

    def _loop_alive(self, frames):
        for frame in reversed(frames):
            if frame[0] == "loop":
                return ~frame[2] & ~frame[3]
        return numpy.ones(self.n, dtype=bool)

    def _test(self, ins):
        value = self.read(ins.operands[0], "u")[:, 0]
        return value != 0 if ins.nonzero else value == 0

    def _compute(self, ins: Instruction):
        code, ops, sat = ins.opcode, ins.operands, ins.saturate
        r = self.read
        if code in _FLOAT2:
            self.write(ops[0], _FLOAT2[code](r(ops[1]), r(ops[2])), sat)
        elif code in _FLOAT1:
            self.write(ops[0], _FLOAT1[code](r(ops[1])), sat)
        elif code == OPS["MAD"]:
            self.write(ops[0], r(ops[1]) * r(ops[2]) + r(ops[3]), sat)
        elif code in (OPS["DP2"], OPS["DP3"], OPS["DP4"]):
            width = {OPS["DP2"]: 2, OPS["DP3"]: 3, OPS["DP4"]: 4}[code]
            self.write(ops[0], (r(ops[1])[:, :width] * r(ops[2])[:, :width]).sum(axis=1), sat)
        elif code in _FCOMPARE:
            self.write(ops[0], numpy.where(_FCOMPARE[code](r(ops[1]), r(ops[2])), 0xFFFFFFFF, 0).astype(numpy.uint32))
        elif code in _ICOMPARE:
            kind = "u" if code in (OPS["ULT"], OPS["UGE"]) else "i"
            self.write(ops[0], numpy.where(_ICOMPARE[code](r(ops[1], kind), r(ops[2], kind)), 0xFFFFFFFF, 0).astype(numpy.uint32))
        elif code in _INT2:
            kind = "u" if code in (OPS["UMAX"], OPS["UMIN"], OPS["USHR"]) else "i"
            self.write(ops[0], (_INT2[code](r(ops[1], kind), r(ops[2], kind)).astype(numpy.int64) & 0xFFFFFFFF).astype(numpy.uint32))
        elif code == OPS["MOV"]:
            value = self._raw(ops[1])[:, list(ops[1].swizzle)]
            if ops[1].modifier or sat:
                value = r(ops[1])
            self.write(ops[0], value, sat)
        elif code == OPS["MOVC"]:
            condition = r(ops[1], "u") != 0
            a, b = self._raw(ops[2])[:, list(ops[2].swizzle)], self._raw(ops[3])[:, list(ops[3].swizzle)]
            if ops[2].modifier or ops[3].modifier or sat:
                a, b = _u(r(ops[2])), _u(r(ops[3]))
            value = numpy.where(condition, a, b).astype(numpy.uint32)
            self.write(ops[0], _f(value).copy() if sat else value, sat)
        elif code == OPS["SWAPC"]:
            condition = r(ops[2], "u") != 0
            a, b = r(ops[3], "u"), r(ops[4], "u")
            first, second = numpy.where(condition, b, a), numpy.where(condition, a, b)
            self.write(ops[0], first)
            self.write(ops[1], second)
        elif code == OPS["SINCOS"]:
            x = r(ops[2])
            self.write(ops[0], numpy.sin(x), sat)
            self.write(ops[1], numpy.cos(x), sat)
        elif code in (OPS["IMUL"], OPS["UMUL"]):
            kind = "i" if code == OPS["IMUL"] else "u"
            product = r(ops[2], kind).astype(numpy.int64) * r(ops[3], kind).astype(numpy.int64)
            if code == OPS["UMUL"]:
                product = r(ops[2], "u").astype(numpy.uint64) * r(ops[3], "u").astype(numpy.uint64)
            self.write(ops[0], (product >> 32).astype(numpy.uint64).astype(numpy.uint32)
                       if code == OPS["UMUL"] else ((product >> 32) & 0xFFFFFFFF).astype(numpy.uint32))
            self.write(ops[1], (product & 0xFFFFFFFF).astype(numpy.uint32))
        elif code in (OPS["IMAD"], OPS["UMAD"]):
            kind = "i" if code == OPS["IMAD"] else "u"
            value = r(ops[1], kind).astype(numpy.int64) * r(ops[2], kind).astype(numpy.int64) + r(ops[3], kind).astype(numpy.int64)
            self.write(ops[0], (value & 0xFFFFFFFF).astype(numpy.uint32))
        elif code == OPS["UDIV"]:
            a, b = r(ops[2], "u"), r(ops[3], "u")
            safe = numpy.where(b == 0, 1, b)
            self.write(ops[0], numpy.where(b == 0, 0xFFFFFFFF, a // safe).astype(numpy.uint32))
            self.write(ops[1], numpy.where(b == 0, 0xFFFFFFFF, a % safe).astype(numpy.uint32))
        elif code == OPS["NOT"]:
            self.write(ops[0], ~r(ops[1], "u"))
        elif code == OPS["INEG"]:
            self.write(ops[0], -r(ops[1], "i"))
        elif code == OPS["ITOF"]:
            self.write(ops[0], r(ops[1], "i").astype(numpy.float32), sat)
        elif code == OPS["UTOF"]:
            self.write(ops[0], r(ops[1], "u").astype(numpy.float32), sat)
        elif code == OPS["FTOI"]:
            self.write(ops[0], numpy.clip(numpy.nan_to_num(numpy.trunc(r(ops[1]))), -2 ** 31, 2 ** 31 - 1).astype(numpy.int64)
                       .astype(numpy.int32))
        elif code == OPS["FTOU"]:
            self.write(ops[0], numpy.clip(numpy.nan_to_num(numpy.trunc(r(ops[1]))), 0, 2 ** 32 - 1).astype(numpy.uint64)
                       .astype(numpy.uint32))
        elif code == OPS["F32TOF16"]:
            self.write(ops[0], r(ops[1]).astype(numpy.float16).view(numpy.uint16).astype(numpy.uint32))
        elif code == OPS["F16TOF32"]:
            self.write(ops[0], (r(ops[1], "u") & 0xFFFF).astype(numpy.uint16).view(numpy.float16).astype(numpy.float32), sat)
        elif code == OPS["COUNTBITS"]:
            value = r(ops[1], "u")
            self.write(ops[0], numpy.array([[bin(int(x)).count("1") for x in row] for row in value], dtype=numpy.uint32))
        elif code in (OPS["UBFE"], OPS["IBFE"]):
            width, offset, value = r(ops[1], "u") & 31, r(ops[2], "u") & 31, r(ops[3], "u").astype(numpy.uint64)
            shifted = (value >> offset.astype(numpy.uint64)) & ((numpy.uint64(1) << width.astype(numpy.uint64)) - numpy.uint64(1))
            if code == OPS["IBFE"]:
                sign = numpy.uint64(1) << (width.astype(numpy.uint64) - numpy.uint64(1)) % numpy.uint64(64)
                shifted = numpy.where((width > 0) & ((shifted & sign) != 0), shifted | ~((sign << numpy.uint64(1)) - numpy.uint64(1)), shifted)
            self.write(ops[0], numpy.where(width == 0, 0, shifted & numpy.uint64(0xFFFFFFFF)).astype(numpy.uint32))
        elif code == OPS["BFI"]:
            width, offset = r(ops[1], "u") & 31, r(ops[2], "u") & 31
            insert, base = r(ops[3], "u").astype(numpy.uint64), r(ops[4], "u").astype(numpy.uint64)
            mask = ((numpy.uint64(1) << width.astype(numpy.uint64)) - numpy.uint64(1)) << offset.astype(numpy.uint64)
            self.write(ops[0], (((insert << offset.astype(numpy.uint64)) & mask) | (base & ~mask)).astype(numpy.uint64)
                       .astype(numpy.uint32))
        elif code == OPS["BFREV"]:
            value = r(ops[1], "u")
            self.write(ops[0], numpy.array([[int(f"{int(x):032b}"[::-1], 2) for x in row] for row in value], dtype=numpy.uint32))
        elif code in (OPS["DERIV_RTX"], OPS["DERIV_RTY"], OPS["DERIV_RTX_COARSE"], OPS["DERIV_RTX_FINE"],
                      OPS["DERIV_RTY_COARSE"], OPS["DERIV_RTY_FINE"]):
            self.write(ops[0], numpy.zeros((self.n, 4), dtype=numpy.float32), sat)
        elif code in (OPS["SAMPLE"], OPS["SAMPLE_L"], OPS["SAMPLE_B"], OPS["SAMPLE_C_LZ"], OPS["SAMPLE_C"], OPS["SAMPLE_D"]):
            self._sample(ins)
        elif code == OPS["LD"]:
            texture = self.textures.get(ops[2].indices[0][0])
            coord = r(ops[1], "i")
            if texture is None:
                value = numpy.zeros((self.n, 4), dtype=numpy.float32)
            else:
                h, w = texture.shape[:2]
                inside = (coord[:, 0] >= 0) & (coord[:, 0] < w) & (coord[:, 1] >= 0) & (coord[:, 1] < h)
                value = texture[numpy.clip(coord[:, 1], 0, h - 1), numpy.clip(coord[:, 0], 0, w - 1)] * inside[:, None]
            self.write(ops[0], value[:, list(ops[2].swizzle)], sat)
        elif code == OPS["RESINFO"]:
            texture = self.textures.get(ops[2].indices[0][0])
            h, w = texture.shape[:2] if texture is not None else (0, 0)
            value = numpy.broadcast_to(numpy.array([w, h, 1, 1], dtype=numpy.float32), (self.n, 4))[:, list(ops[2].swizzle)]
            if ins.control & 3 == 2:  # resinfo_uint
                self.write(ops[0], value.astype(numpy.uint32))
            else:
                self.write(ops[0], value, sat)
        elif code == OPS["NOP"] or code == OPS["LABEL"]:
            pass
        else:
            raise Unsupported(f"instruction {NAMES.get(code, code)}")

    def _sample(self, ins: Instruction):
        ops = ins.operands
        texture = self.textures.get(ops[2].indices[0][0])
        if texture is None:
            value = numpy.zeros((self.n, 4), dtype=numpy.float32)
        else:
            from .raster import sample
            linear, address = self.samplers.get(ops[3].indices[0][0], (False, "clamp"))
            coord = self.read(ops[1])
            value = sample(texture, coord[:, 0], coord[:, 1], linear, address).astype(numpy.float32)
            if ins.opcode in (OPS["SAMPLE_C"], OPS["SAMPLE_C_LZ"]):
                reference = self.read(ops[4])[:, 0]
                value = numpy.repeat((reference <= value[:, 0]).astype(numpy.float32)[:, None], 4, axis=1)
        self.write(ops[0], value[:, list(ops[2].swizzle)], ins.saturate)


def _round_ne(x):
    return numpy.round(x)


_FLOAT2 = {OPS["ADD"]: numpy.add, OPS["MUL"]: numpy.multiply, OPS["DIV"]: numpy.divide, OPS["MIN"]: numpy.fmin,
           OPS["MAX"]: numpy.fmax}
_FLOAT1 = {OPS["FRC"]: lambda x: x - numpy.floor(x), OPS["ROUND_NE"]: _round_ne, OPS["ROUND_NI"]: numpy.floor,
           OPS["ROUND_PI"]: numpy.ceil, OPS["ROUND_Z"]: numpy.trunc, OPS["SQRT"]: numpy.sqrt,
           OPS["RSQ"]: lambda x: 1 / numpy.sqrt(x), OPS["EXP"]: numpy.exp2, OPS["LOG"]: numpy.log2,
           OPS["RCP"]: lambda x: 1 / x}
_FCOMPARE = {OPS["EQ"]: numpy.equal, OPS["NE"]: numpy.not_equal, OPS["LT"]: numpy.less, OPS["GE"]: numpy.greater_equal}
_ICOMPARE = {OPS["IEQ"]: numpy.equal, OPS["INE"]: numpy.not_equal, OPS["ILT"]: numpy.less, OPS["IGE"]: numpy.greater_equal,
             OPS["ULT"]: numpy.less, OPS["UGE"]: numpy.greater_equal}
_INT2 = {OPS["IADD"]: lambda a, b: a.astype(numpy.int64) + b, OPS["AND"]: numpy.bitwise_and, OPS["OR"]: numpy.bitwise_or,
         OPS["XOR"]: numpy.bitwise_xor, OPS["ISHL"]: lambda a, b: a.astype(numpy.int64) << (b & 31),
         OPS["ISHR"]: lambda a, b: a >> (b & 31), OPS["USHR"]: lambda a, b: a >> (b & 31), OPS["IMAX"]: numpy.maximum,
         OPS["IMIN"]: numpy.minimum, OPS["UMAX"]: numpy.maximum, OPS["UMIN"]: numpy.minimum}
_SUPPORTED = set(_FLOAT2) | set(_FLOAT1) | set(_FCOMPARE) | set(_ICOMPARE) | set(_INT2) | {
    OPS[k] for k in ("MAD", "DP2", "DP3", "DP4", "MOV", "MOVC", "SWAPC", "SINCOS", "IMUL", "UMUL", "IMAD", "UMAD", "UDIV",
                     "NOT", "INEG", "ITOF", "UTOF", "FTOI", "FTOU", "F32TOF16", "F16TOF32", "COUNTBITS", "UBFE", "IBFE",
                     "BFI", "BFREV", "DERIV_RTX", "DERIV_RTY", "DERIV_RTX_COARSE", "DERIV_RTX_FINE", "DERIV_RTY_COARSE",
                     "DERIV_RTY_FINE", "SAMPLE", "SAMPLE_L", "SAMPLE_B", "SAMPLE_C_LZ", "SAMPLE_C", "SAMPLE_D", "LD",
                     "RESINFO", "NOP", "IF", "ELSE", "ENDIF", "LOOP", "ENDLOOP", "BREAK", "BREAKC", "CONTINUE",
                     "CONTINUEC", "RET", "RETC", "DISCARD")}
