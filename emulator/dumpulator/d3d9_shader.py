# Fathom addition to dumpulator: an interpreter of the shaders of Direct3D 9 (vs_1_1 to vs_3_0, ps_1_1 to
# ps_3_0) for d3d9_emu.py. Boost Software License 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# Like dxbc.py, a shader runs on all its lanes at once (the vertices of a draw, the fragments of a batch):
# a register is an (n, 4) float array. Covered: the arithmetic and matrix instructions, lit / dst /
# sincos / nrm / crs / sgn / pow / exp / log, the address register (mova, relative constants), the
# constants (def, defi, defb and those set by the program), the control flow (if / ifc / else, rep,
# loop with aL, break / breakc / breakp, call / callnz / label / ret, predicates and setp), the
# texture instructions (texld with texldp / texldb, texldl, texldd, ps 1.x tex / texcoord / texcrd,
# texkill), cmp / cnd / lrp / dp2add, the source modifiers and result shifts of ps 1.x. dsx / dsy and the mip
# level of texld / texldb / texldd come from the 2x2 quads of the pixel stage (raster.derivative);
# the bump-mapping and matrix-texture instructions of ps 1.x (texbem, texm3x3...) are not run.

import struct
from typing import Dict, List, Optional

import numpy

from .dxbc import Unsupported

# opcodes (D3DSHADER_INSTRUCTION_OPCODE_TYPE)
NAMES = {0: "nop", 1: "mov", 2: "add", 3: "sub", 4: "mad", 5: "mul", 6: "rcp", 7: "rsq", 8: "dp3", 9: "dp4", 10: "min",
         11: "max", 12: "slt", 13: "sge", 14: "exp", 15: "log", 16: "lit", 17: "dst", 18: "lrp", 19: "frc", 20: "m4x4",
         21: "m4x3", 22: "m3x4", 23: "m3x3", 24: "m3x2", 25: "call", 26: "callnz", 27: "loop", 28: "ret", 29: "endloop",
         30: "label", 31: "dcl", 32: "pow", 33: "crs", 34: "sgn", 35: "abs", 36: "nrm", 37: "sincos", 38: "rep",
         39: "endrep", 40: "if", 41: "ifc", 42: "else", 43: "endif", 44: "break", 45: "breakc", 46: "mova", 47: "defb",
         48: "defi", 64: "texcoord", 65: "texkill", 66: "tex", 67: "texbem", 68: "texbeml", 69: "texreg2ar",
         70: "texreg2gb", 71: "texm3x2pad", 72: "texm3x2tex", 73: "texm3x3pad", 74: "texm3x3tex", 76: "texm3x3spec",
         77: "texm3x3vspec", 78: "expp", 79: "logp", 80: "cnd", 81: "def", 82: "texreg2rgb", 83: "texdp3tex",
         84: "texm3x2depth", 85: "texdp3", 86: "texm3x3", 87: "texdepth", 88: "cmp", 89: "bem", 90: "dp2add", 91: "dsx",
         92: "dsy", 93: "texldd", 94: "setp", 95: "texldl", 96: "breakp", 0xFFFD: "phase"}
# the parameters of each opcode in shader model 1 (no length in the token): (destinations, sources)
SM1_PARAMS = {0: 0, 1: 2, 2: 3, 3: 3, 4: 4, 5: 3, 6: 2, 7: 2, 8: 3, 9: 3, 10: 3, 11: 3, 12: 3, 13: 3, 14: 2, 15: 2, 16: 2,
              17: 3, 18: 4, 19: 2, 20: 3, 21: 3, 22: 3, 23: 3, 24: 3, 31: 2, 32: 3, 33: 3, 34: 4, 35: 2, 36: 2, 37: 4, 64: 1,
              65: 1, 66: 1, 67: 2, 68: 2, 69: 2, 70: 2, 71: 2, 72: 2, 73: 2, 74: 2, 76: 3, 77: 2, 78: 2, 79: 2, 80: 4,
              81: 5, 82: 2, 83: 2, 84: 2, 85: 2, 86: 2, 87: 1, 88: 4, 89: 3, 90: 4, 0xFFFD: 0}
TEMP, INPUT, CONST, ADDR, RASTOUT, ATTROUT, OUTPUT, CONSTINT, COLOROUT, DEPTHOUT, SAMPLER, CONST2, CONST3, CONST4, \
    CONSTBOOL, LOOP, TEMPFLOAT16, MISCTYPE, LABEL, PREDICATE = range(20)
TEXTURE = ADDR   # in a pixel shader
# usages (D3DDECLUSAGE)
POSITION, BLENDWEIGHT, BLENDINDICES, NORMAL, PSIZE, TEXCOORD, TANGENT, BINORMAL, TESSFACTOR, POSITIONT, COLOR, FOG, \
    DEPTH, SAMPLE = range(14)
COMPARE = {1: numpy.greater, 2: numpy.equal, 3: numpy.greater_equal, 4: numpy.less, 5: numpy.not_equal, 6: numpy.less_equal}


class Param:
    __slots__ = ("type", "number", "mask", "swizzle", "modifier", "result", "shift", "relative")

    def __repr__(self):
        return f"Param({self.type}, {self.number}, mask={self.mask:x}, swizzle={self.swizzle})"


def _param(tokens, i, source: bool, sm2: bool):
    token = tokens[i]
    p = Param()
    p.type = ((token >> 28) & 7) | ((token >> 8) & 0x18)
    p.number = token & 0x7FF
    p.relative = None
    if source:
        p.swizzle = tuple((token >> (16 + 2 * k)) & 3 for k in range(4))
        p.modifier = (token >> 24) & 0xF
        p.mask, p.result, p.shift = 0xF, 0, 0
    else:
        p.mask = (token >> 16) & 0xF
        p.result = (token >> 20) & 0xF
        shift = (token >> 24) & 0xF
        p.shift = shift - 16 if shift & 8 else shift
        p.swizzle, p.modifier = (0, 1, 2, 3), 0
    i += 1
    if token & (1 << 13):  # relative addressing
        if sm2:
            rel = Param()
            t = tokens[i]
            rel.type = ((t >> 28) & 7) | ((t >> 8) & 0x18)
            rel.number = t & 0x7FF
            rel.swizzle = tuple((t >> (16 + 2 * k)) & 3 for k in range(4))
            rel.modifier, rel.mask, rel.result, rel.shift, rel.relative = 0, 0xF, 0, 0, None
            p.relative = rel
            i += 1
        else:  # vs_1_1: a0.x
            rel = Param()
            rel.type, rel.number, rel.swizzle, rel.modifier, rel.mask, rel.result, rel.shift, rel.relative = \
                ADDR, 0, (0, 0, 0, 0), 0, 0xF, 0, 0, None
            p.relative = rel
    return p, i


class Instruction9:
    __slots__ = ("opcode", "control", "dst", "srcs", "predicate", "jump", "extra")

    def __init__(self, opcode, control, dst, srcs, predicate):
        self.opcode, self.control, self.dst, self.srcs, self.predicate = opcode, control, dst, srcs, predicate
        self.jump, self.extra = None, None


class Shader9:
    """A Direct3D 9 shader: kind ("vs" / "ps"), version, declarations, program."""

    def __init__(self, data: bytes):
        count = len(data) // 4
        tokens = struct.unpack(f"<{count}I", data[:count * 4])
        if not tokens:
            raise Unsupported("an empty Direct3D 9 shader")
        version = tokens[0]
        self.kind = "vs" if version >> 16 == 0xFFFE else "ps" if version >> 16 == 0xFFFF else None
        if self.kind is None:
            raise Unsupported("not a Direct3D 9 shader")
        self.major, self.minor = (version >> 8) & 0xFF, version & 0xFF
        sm2 = self.major >= 2
        self.declarations: Dict[tuple, tuple] = {}   # (type, number) -> (usage, index, mask) or sampler type
        self.samplers: Dict[int, int] = {}
        self.defs: Dict[int, tuple] = {}
        self.int_defs: Dict[int, tuple] = {}
        self.bool_defs: Dict[int, bool] = {}
        self.program: List[Instruction9] = []
        self.labels: Dict[int, int] = {}
        i = 1
        while i < count:
            token = tokens[i]
            opcode = token & 0xFFFF
            if opcode == 0xFFFE:      # comment
                i += 1 + ((token >> 16) & 0x7FFF)
                continue
            if opcode == 0xFFFF:      # end
                break
            control = (token >> 16) & 0xFF
            predicated = bool(token & (1 << 28))
            if sm2:
                length = (token >> 24) & 0xF
            elif opcode == 31:
                length = 2
            else:
                length = SM1_PARAMS.get(opcode)
                if length is None:
                    raise Unsupported(f"Direct3D 9 shader instruction {opcode}")
            end = i + 1 + length
            j = i + 1
            if opcode == 31:          # dcl
                info = tokens[j]
                dst, _ = _param(tokens, j + 1, False, sm2)
                if dst.type == SAMPLER:
                    self.samplers[dst.number] = (info >> 27) & 0xF
                else:
                    self.declarations[(dst.type, dst.number)] = (info & 0x1F, (info >> 16) & 0xF, dst.mask)
            elif opcode == 81:        # def c#, 4 floats
                dst, _ = _param(tokens, j, False, sm2)
                self.defs[dst.number] = struct.unpack("<4f", struct.pack("<4I", *tokens[j + 1:j + 5]))
            elif opcode == 48:        # defi
                dst, _ = _param(tokens, j, False, sm2)
                self.int_defs[dst.number] = struct.unpack("<4i", struct.pack("<4I", *tokens[j + 1:j + 5]))
            elif opcode == 47:        # defb
                dst, _ = _param(tokens, j, False, sm2)
                self.bool_defs[dst.number] = bool(tokens[j + 1])
            else:
                dst, srcs, predicate = None, [], None
                if opcode in _DESTINATION:
                    dst, j = _param(tokens, j, False, sm2)
                if predicated:
                    predicate, j = _param(tokens, j, True, sm2)
                while j < end:
                    src, j = _param(tokens, j, True, sm2)
                    srcs.append(src)
                if opcode == 30 and srcs:
                    self.labels[srcs[0].number] = len(self.program)
                self.program.append(Instruction9(opcode, control, dst, srcs, predicate))
            i = end
        self._link()
        unsupported = sorted({NAMES.get(ins.opcode, str(ins.opcode)) for ins in self.program if ins.opcode in _UNSUPPORTED})
        self.error = ("instructions " + ", ".join(unsupported)) if unsupported else None

    def uses_derivatives(self) -> bool:
        """dsx / dsy (texld takes them only for a mipmapped texture, which the device knows)."""
        return any(ins.opcode in (91, 92) for ins in self.program)

    def _link(self):
        stack = []
        for index, ins in enumerate(self.program):
            if ins.opcode in (27, 38, 40, 41):
                stack.append(index)
            elif ins.opcode == 42 and stack:
                self.program[stack[-1]].jump = index
                stack[-1] = index
            elif ins.opcode in (29, 39, 43) and stack:
                opening = stack.pop()
                self.program[opening].jump = index
                ins.jump = opening
        starts = sorted(self.labels.values())
        self.main_end = starts[0] if starts else len(self.program)
        self.label_end = {}
        for k, start in enumerate(starts):
            self.label_end[start] = starts[k + 1] if k + 1 < len(starts) else len(self.program)

    # the semantics of the registers ------------------------------------------
    def input_semantics(self) -> Dict[tuple, tuple]:
        """(register type, number) -> (usage, index) of the inputs."""
        if self.kind == "ps" and self.major < 3:  # v0 / v1: the colors, t#: the texture coordinates
            out = {(INPUT, number): (COLOR, number) for number in range(2)}
            out.update({(TEXTURE, number): (TEXCOORD, number) for number in range(8)})
            return out
        return {(kind, number): (usage, index) for (kind, number), (usage, index, mask) in self.declarations.items()
                if kind == INPUT}

    def output_semantics(self) -> Dict[tuple, tuple]:
        """(register type, number) -> (usage, index) of a vertex shader's outputs."""
        if self.major >= 3:
            return {(kind, number): (usage, index) for (kind, number), (usage, index, mask) in self.declarations.items()
                    if kind == OUTPUT}
        out = {(RASTOUT, 0): (POSITION, 0), (RASTOUT, 1): (FOG, 0), (RASTOUT, 2): (PSIZE, 0),
               (ATTROUT, 0): (COLOR, 0), (ATTROUT, 1): (COLOR, 1)}
        for number in range(8):
            out[(OUTPUT, number)] = (TEXCOORD, number)
        return out


_DESTINATION = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 32, 33, 34, 35, 36,
                37, 46, 64, 66, 67, 68, 69, 70, 71, 72, 73, 74, 76, 77, 78, 79, 80, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91,
                92, 93, 94, 95, 65}
_UNSUPPORTED = {67, 68, 69, 70, 71, 72, 73, 74, 76, 77, 82, 83, 84, 86, 87, 89}


class Machine9:
    """A run of a Direct3D 9 shader on n lanes. inputs: (register type, number) -> (n, 4) floats;
    constants: c (k, 4), i (16, 4), b (16,); textures: s# -> function(coords (n, 4), lod or None) ->
    (n, 4)."""

    def __init__(self, shader: Shader9, n, inputs, constants, int_constants, bool_constants, textures, system=None):
        self.s = shader
        self.n = n
        self.inputs = inputs
        self.c = numpy.array(constants, dtype=numpy.float64, copy=True)
        for number, values in shader.defs.items():
            if number >= len(self.c):
                self.c = numpy.concatenate([self.c, numpy.zeros((number + 1 - len(self.c), 4))])
            self.c[number] = values
        self.i = numpy.array(int_constants, dtype=numpy.int64, copy=True)
        for number, values in shader.int_defs.items():
            self.i[number] = values
        self.b = numpy.array(bool_constants, dtype=bool, copy=True)
        for number, value in shader.bool_defs.items():
            self.b[number] = value
        self.textures = textures
        self.system = system or {}
        self.temps: Dict[int, numpy.ndarray] = {}
        self.outputs: Dict[tuple, numpy.ndarray] = {}
        self.address = numpy.zeros((n, 4), dtype=numpy.float64)
        self.loop_counter = numpy.zeros(n, dtype=numpy.int64)
        self.predicate = numpy.zeros((n, 4), dtype=bool)
        self.active = numpy.ones(n, dtype=bool)
        self.killed = numpy.zeros(n, dtype=bool)
        self.depth = None
        self.ps1 = shader.kind == "ps" and shader.major < 2
        if self.ps1:  # the t registers start with the texture coordinates
            for number in range(8):
                value = inputs.get((TEXTURE, number))
                if value is not None:
                    self.temps[("t", number)] = numpy.array(value, dtype=numpy.float64)

    # registers -----------------------------------------------------------
    def _relative(self, p: Param):
        if p.relative is None:
            return 0
        rel = p.relative
        if rel.type == LOOP:
            return self.loop_counter
        source = self.address if rel.type == ADDR else self._get(rel)
        return numpy.floor(source[:, rel.swizzle[0]] + 0.5).astype(numpy.int64) if rel.type == ADDR else \
            source[:, rel.swizzle[0]].astype(numpy.int64)

    def _get(self, p: Param) -> numpy.ndarray:
        n = self.n
        kind = p.type
        if kind == TEMP:
            return self.temps.setdefault(p.number, numpy.zeros((n, 4)))
        if kind in (CONST, CONST2, CONST3, CONST4):
            base = p.number + {CONST: 0, CONST2: 2048, CONST3: 4096, CONST4: 6144}[kind]
            offset = self._relative(p)
            if isinstance(offset, numpy.ndarray):
                return self.c[numpy.clip(base + offset, 0, len(self.c) - 1)]
            return numpy.broadcast_to(self.c[min(base, len(self.c) - 1)], (n, 4))
        if kind == CONSTINT:
            return numpy.broadcast_to(self.i[p.number].astype(numpy.float64), (n, 4))
        if kind == CONSTBOOL:
            return numpy.full((n, 4), float(self.b[p.number]))
        if kind == ADDR and self.s.kind == "vs":
            return self.address
        if kind == TEXTURE and self.s.kind == "ps":
            if self.ps1:
                return self.temps.setdefault(("t", p.number), numpy.zeros((n, 4)))
            value = self.inputs.get((TEXTURE, p.number))
            return value if value is not None else numpy.zeros((n, 4))
        if kind == INPUT:
            offset = self._relative(p)
            if isinstance(offset, numpy.ndarray):
                out = numpy.zeros((n, 4))
                for value in numpy.unique(offset):
                    lanes = offset == value
                    source = self.inputs.get((INPUT, p.number + int(value)))
                    if source is not None:
                        out[lanes] = source[lanes]
                return out
            value = self.inputs.get((INPUT, p.number))
            return value if value is not None else numpy.zeros((n, 4))
        if kind == MISCTYPE:
            value = self.system.get("vpos" if p.number == 0 else "vface")
            return value if value is not None else numpy.zeros((n, 4))
        if kind == LOOP:
            return numpy.repeat(self.loop_counter[:, None].astype(numpy.float64), 4, axis=1)
        if kind == PREDICATE:
            return self.predicate.astype(numpy.float64)
        if kind in (RASTOUT, ATTROUT, OUTPUT, COLOROUT):
            return self.outputs.setdefault((kind, p.number), numpy.zeros((n, 4)))
        if kind == TEMPFLOAT16:
            return self.temps.setdefault(("h", p.number), numpy.zeros((n, 4)))
        raise Unsupported(f"Direct3D 9 register type {kind}")

    def read(self, p: Param) -> numpy.ndarray:
        value = self._get(p)[:, list(p.swizzle)].astype(numpy.float64)
        m = p.modifier
        if m == 0:
            return value
        if m == 1:
            return -value
        if m == 2:
            return value - 0.5
        if m == 3:
            return -(value - 0.5)
        if m == 4:
            return 2 * (value - 0.5)
        if m == 5:
            return -2 * (value - 0.5)
        if m == 6:
            return 1 - value
        if m == 7:
            return 2 * value
        if m == 8:
            return -2 * value
        if m == 9:
            return value / numpy.where(value[:, 2:3] == 0, 1, value[:, 2:3])
        if m == 10:
            return value / numpy.where(value[:, 3:4] == 0, 1, value[:, 3:4])
        if m == 11:
            return numpy.abs(value)
        if m == 12:
            return -numpy.abs(value)
        if m == 13:
            return (value == 0).astype(numpy.float64)
        return value

    def write(self, p: Param, value, predicate: Optional[Param] = None):
        value = numpy.asarray(value, dtype=numpy.float64)
        if value.ndim == 1:
            value = numpy.repeat(value[:, None], 4, axis=1)
        if p.shift:
            value = value * (2.0 ** p.shift)
        if p.result & 1:
            value = numpy.clip(numpy.nan_to_num(value), 0, 1)
        if self.ps1 and p.type == TEMP:
            value = numpy.clip(value, -8, 8)
        lanes = self.active
        component_mask = numpy.ones((self.n, 4), dtype=bool)
        if predicate is not None:
            pred = self.predicate[:, list(predicate.swizzle)]
            if predicate.modifier == 13:
                pred = ~pred
            component_mask = pred
        if p.type == ADDR and self.s.kind == "vs":
            target = self.address
            value = numpy.floor(value + 0.5) if self.s.major >= 2 else numpy.floor(value)
        elif p.type == DEPTHOUT:
            if self.depth is None:
                self.depth = numpy.zeros(self.n)
            self.depth[lanes] = value[lanes, 0]
            return
        elif p.type == PREDICATE:
            for c in range(4):
                if p.mask >> c & 1:
                    sel = lanes & component_mask[:, c]
                    self.predicate[sel, c] = value[sel, c] != 0
            return
        elif p.type == TEXTURE and self.s.kind == "ps":
            target = self.temps.setdefault(("t", p.number), numpy.zeros((self.n, 4)))
        elif p.type == TEMPFLOAT16:
            target = self.temps.setdefault(("h", p.number), numpy.zeros((self.n, 4)))
        else:
            target = self._get(p)
            if not target.flags.writeable:
                raise Unsupported(f"write to the Direct3D 9 register type {p.type}")
        for c in range(4):
            if p.mask >> c & 1:
                sel = lanes & component_mask[:, c]
                target[sel, c] = value[sel, c]

    # execution -----------------------------------------------------------
    def run(self):
        program = self.s.program
        frames = []      # [kind, previous active, broken, remaining / call return, ...]
        pc = 0
        steps = 0
        with numpy.errstate(all="ignore"):
            while pc < len(program):
                if pc == self.s.main_end and not any(f[0] == "call" for f in frames):
                    break
                ins = program[pc]
                code = ins.opcode
                steps += 1
                if steps > 1 << 22:
                    raise Unsupported("a Direct3D 9 shader that does not end")
                if code in (40, 41):         # if b# / if_comp a, b / if p0
                    if code == 41:
                        condition = COMPARE.get(ins.control, numpy.greater)(self.read(ins.srcs[0])[:, 0], self.read(ins.srcs[1])[:, 0])
                    else:
                        src = ins.srcs[0]
                        if src.type == PREDICATE:
                            condition = self.predicate[:, src.swizzle[0]]
                            if src.modifier == 13:
                                condition = ~condition
                        else:
                            condition = self.read(src)[:, 0] != 0
                    frames.append(["if", self.active.copy(), condition])
                    self.active = self.active & condition
                elif code == 42:
                    frame = frames[-1]
                    self.active = frame[1] & ~frame[2] & self._alive(frames[:-1])
                elif code == 43:
                    frame = frames.pop()
                    self.active = frame[1] & self._alive(frames)
                elif code in (38, 27):       # rep i# / loop aL, i#
                    counts = self.i[ins.srcs[-1].number]
                    iterations = int(counts[0])
                    frames.append(["loop", self.active.copy(), numpy.zeros(self.n, bool), iterations, pc,
                                   self.loop_counter.copy()])
                    if code == 27:
                        self.loop_counter = numpy.full(self.n, int(counts[1]), dtype=numpy.int64)
                        frames[-1].append(int(counts[2]))
                    if iterations <= 0:
                        frames.pop()
                        pc = ins.jump + 1
                        continue
                elif code in (39, 29):
                    frame = frames[-1]
                    frame[3] -= 1
                    if len(frame) > 6:
                        self.loop_counter = self.loop_counter + frame[6]
                    self.active = frame[1] & ~frame[2] & self._alive(frames[:-1])
                    if frame[3] > 0 and self.active.any():
                        pc = frame[4] + 1
                        continue
                    frames.pop()
                    self.loop_counter = frame[5]
                    self.active = frame[1] & self._alive(frames)
                elif code in (44, 45, 96):   # break, break_comp, breakp
                    if code == 45:
                        hit = COMPARE.get(ins.control, numpy.greater)(self.read(ins.srcs[0])[:, 0], self.read(ins.srcs[1])[:, 0])
                    elif code == 96:
                        src = ins.srcs[0]
                        hit = self.predicate[:, src.swizzle[0]]
                        if src.modifier == 13:
                            hit = ~hit
                    else:
                        hit = numpy.ones(self.n, dtype=bool)
                    hit = hit & self.active
                    loop = next(f for f in reversed(frames) if f[0] == "loop")
                    loop[2] |= hit
                    self.active = self.active & ~hit
                elif code in (25, 26):       # call l#, callnz l#, b# / p0
                    hit = self.active.copy()
                    if code == 26:
                        src = ins.srcs[1]
                        if src.type == PREDICATE:
                            condition = self.predicate[:, src.swizzle[0]]
                        else:
                            condition = numpy.full(self.n, bool(self.b[src.number]))
                        if src.modifier == 13:
                            condition = ~condition
                        hit &= condition
                    start = self.s.labels.get(ins.srcs[0].number)
                    if start is not None and hit.any():
                        frames.append(["call", self.active.copy(), numpy.zeros(self.n, bool), pc + 1,
                                       self.s.label_end[start]])
                        self.active = hit
                        pc = start + 1
                        continue
                elif code == 28:             # ret
                    call = next((f for f in reversed(frames) if f[0] == "call"), None)
                    if call is None:
                        break
                    call[2] |= self.active
                    self.active = numpy.zeros(self.n, dtype=bool)
                elif code in (30, 0, 0xFFFD):
                    pass
                elif code == 65:             # texkill
                    src = ins.dst
                    value = self.read_dst_as_source(src)
                    kill = (value[:, :3] < 0).any(axis=1) & self.active
                    self.killed |= kill
                    self.active = self.active & ~kill
                elif self.active.any():
                    self._compute(ins)
                pc += 1
                while frames and frames[-1][0] == "call" and pc >= frames[-1][4]:
                    frame = frames.pop()
                    self.active = frame[1] & self._alive(frames)
                    pc = frame[3]
        return self

    def read_dst_as_source(self, p: Param):
        return self._get(p)

    def _alive(self, frames):
        alive = ~self.killed
        for frame in frames:
            if frame[0] in ("loop", "call"):
                alive &= ~frame[2]
        return alive

    def _texture(self, stage, coords, lod=None, bias=None, gradients=None):
        function = self.textures.get(stage)
        if function is None:
            out = numpy.zeros((self.n, 4))
            out[:, 3] = 1
            return out
        if bias is None and gradients is None:
            return numpy.asarray(function(coords, lod), dtype=numpy.float64)
        return numpy.asarray(function(coords, lod, bias, gradients), dtype=numpy.float64)

    def _compute(self, ins: Instruction9):
        code, d, s, pred = ins.opcode, ins.dst, ins.srcs, ins.predicate
        r = self.read
        w = lambda value: self.write(d, value, pred)
        if code == 1:
            w(r(s[0]))
        elif code == 2:
            w(r(s[0]) + r(s[1]))
        elif code == 3:
            w(r(s[0]) - r(s[1]))
        elif code == 4:
            w(r(s[0]) * r(s[1]) + r(s[2]))
        elif code == 5:
            w(r(s[0]) * r(s[1]))
        elif code == 6:
            x = r(s[0])[:, 3 if s[0].swizzle == (0, 1, 2, 3) else 0]
            w(numpy.where(x == 0, numpy.inf, 1 / numpy.where(x == 0, 1, x)))
        elif code == 7:
            x = numpy.abs(r(s[0])[:, 3 if s[0].swizzle == (0, 1, 2, 3) else 0])
            w(numpy.where(x == 0, numpy.inf, 1 / numpy.sqrt(numpy.where(x == 0, 1, x))))
        elif code == 8:
            w((r(s[0])[:, :3] * r(s[1])[:, :3]).sum(1))
        elif code == 9:
            w((r(s[0]) * r(s[1])).sum(1))
        elif code == 10:
            w(numpy.minimum(r(s[0]), r(s[1])))
        elif code == 11:
            w(numpy.maximum(r(s[0]), r(s[1])))
        elif code == 12:
            w((r(s[0]) < r(s[1])).astype(numpy.float64))
        elif code == 13:
            w((r(s[0]) >= r(s[1])).astype(numpy.float64))
        elif code in (14, 78):
            w(numpy.exp2(r(s[0])[:, 3 if s[0].swizzle == (0, 1, 2, 3) else 0]))
        elif code in (15, 79):
            x = numpy.abs(r(s[0])[:, 3 if s[0].swizzle == (0, 1, 2, 3) else 0])
            w(numpy.where(x == 0, -3.4e38, numpy.log2(numpy.where(x == 0, 1, x))))
        elif code == 16:
            v = r(s[0])
            out = numpy.zeros((self.n, 4))
            out[:, 0] = 1
            out[:, 1] = numpy.maximum(v[:, 0], 0)
            power = numpy.clip(v[:, 3], -128, 128)
            out[:, 2] = numpy.where(v[:, 0] > 0, numpy.maximum(v[:, 1], 0) ** power, 0)
            out[:, 3] = 1
            w(out)
        elif code == 17:
            a, b = r(s[0]), r(s[1])
            w(numpy.stack([numpy.ones(self.n), a[:, 1] * b[:, 1], a[:, 2], b[:, 3]], 1))
        elif code == 18:
            w(r(s[0]) * (r(s[1]) - r(s[2])) + r(s[2]))
        elif code == 19:
            v = r(s[0])
            w(v - numpy.floor(v))
        elif code in (20, 21, 22, 23, 24):
            rows, width = {20: (4, 4), 21: (3, 4), 22: (4, 3), 23: (3, 3), 24: (2, 3)}[code]
            v = r(s[0])
            out = numpy.zeros((self.n, 4))
            for k in range(rows):
                row = Param()
                for attr in Param.__slots__:
                    setattr(row, attr, getattr(s[1], attr))
                row.number = s[1].number + k
                out[:, k] = (v[:, :width] * r(row)[:, :width]).sum(1)
            mask = d.mask
            d.mask = mask & ((1 << rows) - 1)
            w(out)
            d.mask = mask
        elif code == 32:
            a = numpy.abs(r(s[0])[:, 0])
            w(a ** r(s[1])[:, 0])
        elif code == 33:
            w(numpy.concatenate([numpy.cross(r(s[0])[:, :3], r(s[1])[:, :3]), numpy.zeros((self.n, 1))], 1))
        elif code == 34:
            w(numpy.sign(r(s[0])))
        elif code == 35:
            w(numpy.abs(r(s[0])))
        elif code == 36:
            v = r(s[0])
            length = numpy.sqrt((v[:, :3] ** 2).sum(1, keepdims=True))
            w(v / numpy.where(length == 0, 1, length))
        elif code == 37:
            x = r(s[0])[:, 3 if s[0].swizzle == (0, 1, 2, 3) else 0]
            w(numpy.stack([numpy.cos(x), numpy.sin(x), numpy.zeros(self.n), numpy.zeros(self.n)], 1))
        elif code == 46:
            w(r(s[0]))
        elif code == 80:  # cnd
            w(numpy.where(r(s[0]) > 0.5, r(s[1]), r(s[2])))
        elif code == 88:  # cmp
            w(numpy.where(r(s[0]) >= 0, r(s[1]), r(s[2])))
        elif code == 90:  # dp2add
            w((r(s[0])[:, :2] * r(s[1])[:, :2]).sum(1) + r(s[2])[:, 0])
        elif code in (91, 92):  # dsx / dsy
            from .raster import derivative
            w(derivative(r(s[0]), "x" if code == 91 else "y"))
        elif code == 94:  # setp_comp p0, a, b
            w(COMPARE.get(ins.control, numpy.greater)(r(s[0]), r(s[1])).astype(numpy.float64))
        elif code == 85:  # texdp3
            w((r(s[0])[:, :3] * self._get(d)[:, :3]).sum(1))
        elif code == 64:  # texcoord t# (ps 1.1-1.3) / texcrd r#, t# (ps 1.4)
            if s:
                w(r(s[0]))
            else:
                w(numpy.clip(self.inputs.get((TEXTURE, d.number), numpy.zeros((self.n, 4))), 0, 1))
        elif code == 66:  # tex / texld
            self._texld(ins)
        elif code == 95:  # texldl
            coords = r(s[0])
            w(self._texture(s[1].number, coords, coords[:, 3])[:, list(s[1].swizzle)])
        elif code == 93:  # texldd dst, coords, s#, ddx, ddy
            w(self._texture(s[1].number, r(s[0]), None, None, (r(s[2]), r(s[3])))[:, list(s[1].swizzle)])
        else:
            raise Unsupported(f"Direct3D 9 shader instruction {NAMES.get(code, code)}")

    def _texld(self, ins):
        d, s = ins.dst, ins.srcs
        if self.s.kind == "ps" and self.s.major < 2:
            if self.s.minor < 4:  # tex t#: the stage of t#, its texture coordinates
                coords = self.inputs.get((TEXTURE, d.number), numpy.zeros((self.n, 4)))
                self.write(d, self._texture(d.number, numpy.asarray(coords, dtype=numpy.float64)))
                return
            coords = self.read(s[0])    # texld r#, t# / r#: the stage of r#
            self.write(d, self._texture(d.number, coords))
            return
        coords = self.read(s[0])
        bias = None
        if ins.control & 1:    # texldp
            coords = coords / numpy.where(coords[:, 3:4] == 0, 1, coords[:, 3:4])
        elif ins.control & 2:  # texldb: the bias in w
            bias = coords[:, 3].astype(numpy.float64)
        value = self._texture(s[1].number, coords, None, bias)
        self.write(d, value[:, list(s[1].swizzle)], ins.predicate)


def run(shader: Shader9, n, inputs, constants, int_constants, bool_constants, textures, system=None) -> Machine9:
    if shader.error:
        raise Unsupported(shader.error)
    return Machine9(shader, n, inputs, constants, int_constants, bool_constants, textures, system).run()
