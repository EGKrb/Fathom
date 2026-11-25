# Fathom addition to dumpulator: an interpreter of the shaders of Direct3D 10 / 11 / 12 (DXBC, shader
# models 4.0 to 5.1): vertex, pixel, geometry, hull, domain and compute shaders. Boost Software License
# 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# A shader runs on all its lanes at once (the vertices of a draw, the fragments of a batch, the
# primitives given to a geometry shader, the patches of a hull shader, the points of a domain shader,
# the threads of a dispatch): a register is an (n, 4) array of 32-bit values, an instruction a numpy
# operation, the control flow (if / else, loop / break / continue, switch / case, call / label / ret,
# discard) masks of active lanes. The threads of a compute shader run in lockstep, so a group's
# barriers (sync) hold; the atomic operations happen in the order of the lanes. Covered: the arithmetic
# (float, integer, bits, conversions), constant buffers, the immediate constant buffer, indexable
# temporaries, textures (sample* with offsets, gather4*, ld, resinfo, lod) of every dimension, typed /
# raw / structured buffers and unordered access views, group shared memory, atomics, the geometry
# shader's emit / cut (strips of points, lines, triangles; instancing), the hull shader's phases
# (control point, fork, join). Not covered: doubles, interfaces (fcall), DXIL (shader model 6, the
# LLVM bitcode of dxc, is dxil.py). The derivatives (deriv_rtx / rty, coarse and fine) and the mip level of
# sample / sample_b / sample_c / lod come from the 2x2 quads of the pixel stage (raster.derivative); the
# helper lanes of the quads write no unordered access view.

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
           UDIV=78, ULT=79, UGE=80, UMUL=81, UMAD=82, UMAX=83, UMIN=84, USHR=85, UTOF=86, XOR=87, LOD=108, GATHER4=109,
           SAMPLE_POS=110, SAMPLE_INFO=111, HS_DECLS=113, HS_CONTROL_POINT_PHASE=114, HS_FORK_PHASE=115,
           HS_JOIN_PHASE=116, EMIT_STREAM=117, CUT_STREAM=118, EMITTHENCUT_STREAM=119, INTERFACE_CALL=120, BUFINFO=121,
           DERIV_RTX_COARSE=122, DERIV_RTX_FINE=123, DERIV_RTY_COARSE=124, DERIV_RTY_FINE=125, GATHER4_C=126,
           GATHER4_PO=127, GATHER4_PO_C=128, RCP=129, F32TOF16=130, F16TOF32=131, UADDC=132, USUBB=133, COUNTBITS=134,
           FIRSTBIT_HI=135, FIRSTBIT_LO=136, FIRSTBIT_SHI=137, UBFE=138, IBFE=139, BFI=140, BFREV=141, SWAPC=142,
           LD_UAV_TYPED=163, STORE_UAV_TYPED=164, LD_RAW=165, STORE_RAW=166, LD_STRUCTURED=167, STORE_STRUCTURED=168,
           ATOMIC_AND=169, ATOMIC_OR=170, ATOMIC_XOR=171, ATOMIC_CMP_STORE=172, ATOMIC_IADD=173, ATOMIC_IMAX=174,
           ATOMIC_IMIN=175, ATOMIC_UMAX=176, ATOMIC_UMIN=177, IMM_ATOMIC_ALLOC=178, IMM_ATOMIC_CONSUME=179,
           IMM_ATOMIC_IADD=180, IMM_ATOMIC_AND=181, IMM_ATOMIC_OR=182, IMM_ATOMIC_XOR=183, IMM_ATOMIC_EXCH=184,
           IMM_ATOMIC_CMP_EXCH=185, IMM_ATOMIC_IMAX=186, IMM_ATOMIC_IMIN=187, IMM_ATOMIC_UMAX=188, IMM_ATOMIC_UMIN=189,
           SYNC=190, EVAL_SNAPPED=204, EVAL_SAMPLE_INDEX=205, EVAL_CENTROID=206, ABORT=208, DEBUG_BREAK=209)
NAMES = {v: k for k, v in OPS.items()}
DCL = dict(RESOURCE=88, CONSTANT_BUFFER=89, SAMPLER=90, INDEX_RANGE=91, GS_OUTPUT_TOPOLOGY=92, GS_INPUT_PRIMITIVE=93,
           MAX_OUTPUT_VERTEX_COUNT=94, INPUT=95, INPUT_SGV=96, INPUT_SIV=97, INPUT_PS=98, INPUT_PS_SGV=99,
           INPUT_PS_SIV=100, OUTPUT=101, OUTPUT_SGV=102, OUTPUT_SIV=103, TEMPS=104, INDEXABLE_TEMP=105,
           GLOBAL_FLAGS=106, STREAM=143, FUNCTION_BODY=144, FUNCTION_TABLE=145, INTERFACE=146,
           INPUT_CONTROL_POINT_COUNT=147, OUTPUT_CONTROL_POINT_COUNT=148, TESS_DOMAIN=149, TESS_PARTITIONING=150,
           TESS_OUTPUT_PRIMITIVE=151, HS_MAX_TESSFACTOR=152, HS_FORK_PHASE_INSTANCE_COUNT=153,
           HS_JOIN_PHASE_INSTANCE_COUNT=154, THREAD_GROUP=155, UAV_TYPED=156, UAV_RAW=157, UAV_STRUCTURED=158,
           TGSM_RAW=159, TGSM_STRUCTURED=160, RESOURCE_RAW=161, RESOURCE_STRUCTURED=162, GS_INSTANCE_COUNT=207)
DECLARATIONS = set(DCL.values()) | {107}
PHASES = {OPS["HS_DECLS"], OPS["HS_CONTROL_POINT_PHASE"], OPS["HS_FORK_PHASE"], OPS["HS_JOIN_PHASE"]}

TEMP, INPUT, OUTPUT, INDEXABLE_TEMP, IMM32, IMM64, SAMPLER, RESOURCE, CONSTANT_BUFFER, ICB, LABEL, PRIMITIVE_ID, \
    OUTPUT_DEPTH, NULL, RASTERIZER, OUTPUT_COVERAGE_MASK, STREAM, FUNCTION_BODY, FUNCTION_TABLE, INTERFACE, \
    FUNCTION_INPUT, FUNCTION_OUTPUT, OUTPUT_CONTROL_POINT_ID, INPUT_FORK_INSTANCE_ID, INPUT_JOIN_INSTANCE_ID, \
    INPUT_CONTROL_POINT, OUTPUT_CONTROL_POINT, INPUT_PATCH_CONSTANT, INPUT_DOMAIN_POINT, THIS_POINTER, UAV, TGSM, \
    THREAD_ID, THREAD_GROUP_ID, THREAD_ID_IN_GROUP, INPUT_COVERAGE_MASK, THREAD_ID_IN_GROUP_FLATTENED, \
    GS_INSTANCE_ID, OUTPUT_DEPTH_GE, OUTPUT_DEPTH_LE, CYCLE_COUNTER = range(41)
SYSTEM_OPERANDS = {PRIMITIVE_ID: "primitive_id", OUTPUT_CONTROL_POINT_ID: "output_cp_id",
                   INPUT_FORK_INSTANCE_ID: "fork_instance", INPUT_JOIN_INSTANCE_ID: "join_instance",
                   INPUT_DOMAIN_POINT: "domain", THREAD_ID: "thread_id", THREAD_GROUP_ID: "group_id",
                   THREAD_ID_IN_GROUP: "thread_in_group", THREAD_ID_IN_GROUP_FLATTENED: "flattened",
                   INPUT_COVERAGE_MASK: "coverage", GS_INSTANCE_ID: "gs_instance", CYCLE_COUNTER: "cycle"}
RESOURCE_DIMENSIONS = {1: "buffer", 2: "1d", 3: "2d", 4: "2d", 5: "3d", 6: "cube", 7: "1darray", 8: "2darray",
                       9: "2darray", 10: "cubearray", 11: "raw", 12: "structured"}
MAX_LOOP = 1 << 16
# the system value names of signatures (D3D_NAME)
SV_POSITION, SV_CLIP_DISTANCE, SV_CULL_DISTANCE, SV_RENDER_TARGET_ARRAY_INDEX, SV_VIEWPORT_ARRAY_INDEX, SV_VERTEX_ID, \
    SV_PRIMITIVE_ID, SV_INSTANCE_ID, SV_IS_FRONT_FACE, SV_SAMPLE_INDEX = range(1, 11)
SV_TESS = {11: "quad_edge", 12: "quad_inside", 13: "tri_edge", 14: "tri_inside", 15: "line_detail", 16: "line_density"}
SV_TARGET, SV_DEPTH, SV_COVERAGE = 64, 65, 66


class Operand:
    __slots__ = ("type", "mask", "swizzle", "modifier", "indices", "imm", "components")

    def __repr__(self):
        return f"Operand(type={self.type}, indices={self.indices}, mask={self.mask:x}, swizzle={self.swizzle})"

    @property
    def slot(self) -> int:
        """The register number of a resource / sampler / UAV / constant buffer (5.1: its range id)."""
        return self.indices[0][0] if self.indices else 0


class Instruction:
    __slots__ = ("opcode", "saturate", "nonzero", "control", "operands", "jump", "offsets", "extra")

    def __init__(self, opcode, saturate, nonzero, control, operands, offsets=(0, 0, 0)):
        self.opcode, self.saturate, self.nonzero, self.control = opcode, saturate, nonzero, control
        self.operands, self.jump, self.offsets, self.extra = operands, None, offsets, None


def _operand(tokens, i):
    token = tokens[i]
    i += 1
    op = Operand()
    components = token & 3
    op.components = components
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
        raise Unsupported("64-bit immediates (doubles)")
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
    stride, base = {b"ISG1": (32, 4), b"OSG1": (32, 4), b"PSG1": (32, 4), b"OSG5": (28, 4)}.get(tag, (24, 0))
    out = []
    for k in range(count):
        offset = 8 + k * stride + base
        name, index, sv, kind, register = struct.unpack_from("<5I", chunk, offset)
        mask = chunk[offset + 20]
        stream = struct.unpack_from("<I", chunk, 8 + k * stride)[0] if tag in (b"OSG5", b"OSG1") else 0
        end = chunk.index(b"\0", name)
        out.append(dict(name=chunk[name:end].decode("latin-1").upper(), index=index, sv=sv, kind=kind, register=register,
                        mask=mask, stream=stream))
    return out


class Phase:
    """A program: the main one, or a phase of a hull shader."""

    def __init__(self, kind: str):
        self.kind = kind
        self.program: List[Instruction] = []
        self.temps = 0
        self.indexable: Dict[int, tuple] = {}     # x# -> (size, components)
        self.instances = 1
        self.labels: Dict[int, tuple] = {}        # label -> (start, end)


class Shader:
    """A DXBC shader: kind (0 pixel, 1 vertex, 2 geometry, 3 hull, 4 domain, 5 compute), signatures,
    declarations and program (phases for a hull shader)."""

    KINDS = {0: "pixel", 1: "vertex", 2: "geometry", 3: "hull", 4: "domain", 5: "compute"}

    def __init__(self, bytecode: bytes):
        parts = chunks(bytecode)
        if b"DXIL" in parts and b"SHEX" not in parts and b"SHDR" not in parts:
            raise Unsupported("DXIL (shader model 6) shaders")
        code = parts.get(b"SHEX") or parts.get(b"SHDR")
        if code is None:
            raise Unsupported("no shader code")
        self.inputs = self._signature(parts, (b"ISGN", b"ISG1"))
        self.outputs = self._signature(parts, (b"OSGN", b"OSG1", b"OSG5"))
        self.patch = self._signature(parts, (b"PCSG", b"PSG1"))
        version, length = struct.unpack_from("<2I", code, 0)
        self.kind = version >> 16
        self.model = ((version >> 4) & 0xF, version & 0xF)
        self.sm51 = self.model >= (5, 1)  # resources by range: [id][register] (and a register space)
        self.root_signature = parts.get(b"RTS0")
        tokens = struct.unpack_from(f"<{length}I", code, 0)
        self.icb = None
        self.interpolation: Dict[int, int] = {}
        self.resources: Dict[int, dict] = {}       # t# -> {dimension, stride}
        self.uavs: Dict[int, dict] = {}            # u# -> {dimension, stride}
        self.tgsm: Dict[int, tuple] = {}           # g# -> (bytes, stride or 0)
        self.samplers: Dict[int, int] = {}
        self.thread_group = (1, 1, 1)
        self.gs_input = 3
        self.gs_output = 5
        self.gs_max_vertices = 0
        self.gs_instances = 1
        self.input_cp = 0
        self.output_cp = 0
        self.domain = 2
        self.partitioning = 1
        self.output_primitive = 3
        self.max_tessfactor = 64.0
        self.output_system: Dict[int, int] = {}    # o# -> system value (dcl_output_siv)
        self.phases: List[Phase] = []
        self.error = None
        phase = Phase("main")
        self.phases.append(phase)
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
                size = (token >> 24) & 0x7F
                if size == 0:
                    raise Unsupported(f"opcode {opcode} without a length")
                end = i + size
                if opcode in PHASES:
                    if opcode != OPS["HS_DECLS"]:
                        phase = Phase({OPS["HS_CONTROL_POINT_PHASE"]: "control", OPS["HS_FORK_PHASE"]: "fork",
                                       OPS["HS_JOIN_PHASE"]: "join"}[opcode])
                        if self.phases and self.phases[0].kind == "main" and not self.phases[0].program:
                            self.phases.pop(0)
                        self.phases.append(phase)
                elif opcode in DECLARATIONS:
                    self._declaration(phase, opcode, token, tokens, i, end)
                else:
                    j = i + 1
                    extended = token >> 31
                    offsets = (0, 0, 0)
                    while extended:
                        ext = tokens[j]
                        if ext & 0x3F == 1:  # sample controls: immediate texel offsets
                            offsets = tuple(_signed4((ext >> shift) & 0xF) for shift in (9, 13, 17))
                        extended = ext >> 31
                        j += 1
                    operands = []
                    while j < end:
                        op, j = _operand(tokens, j)
                        operands.append(op)
                    phase.program.append(Instruction(opcode, (token >> 13) & 1, (token >> 18) & 1, (token >> 11) & 0x1FFF,
                                                     operands, offsets))
                i = end
        except (Unsupported, IndexError, struct.error) as e:
            self.error = str(e)
        for p in self.phases:
            self._link(p)
        unsupported = sorted({NAMES.get(ins.opcode, str(ins.opcode)) for p in self.phases for ins in p.program
                              if ins.opcode not in _SUPPORTED})
        if unsupported and self.error is None:
            self.error = "instructions " + ", ".join(unsupported)

    @property
    def main(self) -> Phase:
        return self.phases[0]

    @staticmethod
    def _signature(parts, tags):
        for tag in tags:
            if tag in parts:
                return signature(parts[tag], tag)
        return []

    def _register(self, op) -> int:
        """The register of a declared resource (5.1: the first register of its range)."""
        if self.sm51 and len(op.indices) >= 2:
            return op.indices[1][0]
        return op.slot

    def _declaration(self, phase: Phase, opcode, token, tokens, i, end):
        control = (token >> 11) & 0x1FFF
        if opcode == DCL["TEMPS"]:
            phase.temps = tokens[i + 1]
        elif opcode == DCL["INDEXABLE_TEMP"]:
            phase.indexable[tokens[i + 1]] = (tokens[i + 2], tokens[i + 3] if end - i > 3 else 4)
        elif opcode in (DCL["INPUT_PS"], DCL["INPUT_PS_SIV"], DCL["INPUT_PS_SGV"]):
            op, _ = _operand(tokens, i + 1)
            if op.indices:
                self.interpolation[op.indices[-1][0]] = control & 0xF
        elif opcode in (DCL["OUTPUT_SIV"], DCL["OUTPUT_SGV"]):
            op, j = _operand(tokens, i + 1)
            if op.indices and j < end:
                self.output_system[op.indices[-1][0]] = tokens[j]
        elif opcode == DCL["RESOURCE"]:
            op, _ = _operand(tokens, i + 1)
            self.resources[self._register(op)] = dict(dimension=RESOURCE_DIMENSIONS.get(control & 0x1F, "2d"), stride=0)
        elif opcode == DCL["RESOURCE_RAW"]:
            op, _ = _operand(tokens, i + 1)
            self.resources[self._register(op)] = dict(dimension="raw", stride=0)
        elif opcode == DCL["RESOURCE_STRUCTURED"]:
            op, j = _operand(tokens, i + 1)
            self.resources[self._register(op)] = dict(dimension="structured", stride=tokens[j])
        elif opcode == DCL["UAV_TYPED"]:
            op, _ = _operand(tokens, i + 1)
            self.uavs[self._register(op)] = dict(dimension=RESOURCE_DIMENSIONS.get(control & 0x1F, "2d"), stride=0)
        elif opcode == DCL["UAV_RAW"]:
            op, _ = _operand(tokens, i + 1)
            self.uavs[self._register(op)] = dict(dimension="raw", stride=0)
        elif opcode == DCL["UAV_STRUCTURED"]:
            op, j = _operand(tokens, i + 1)
            self.uavs[self._register(op)] = dict(dimension="structured", stride=tokens[j])
        elif opcode == DCL["TGSM_RAW"]:
            op, j = _operand(tokens, i + 1)
            self.tgsm[op.slot] = (tokens[j], 0)
        elif opcode == DCL["TGSM_STRUCTURED"]:
            op, j = _operand(tokens, i + 1)
            self.tgsm[op.slot] = (tokens[j] * tokens[j + 1], tokens[j])
        elif opcode == DCL["SAMPLER"]:
            op, _ = _operand(tokens, i + 1)
            self.samplers[self._register(op)] = control & 0xF
        elif opcode == DCL["THREAD_GROUP"]:
            self.thread_group = tuple(tokens[i + 1:i + 4])
        elif opcode == DCL["GS_INPUT_PRIMITIVE"]:
            self.gs_input = control & 0x3F
        elif opcode == DCL["GS_OUTPUT_TOPOLOGY"]:
            self.gs_output = control & 0x7F
        elif opcode == DCL["MAX_OUTPUT_VERTEX_COUNT"]:
            self.gs_max_vertices = tokens[i + 1]
        elif opcode == DCL["GS_INSTANCE_COUNT"]:
            self.gs_instances = max(1, tokens[i + 1])
        elif opcode == DCL["INPUT_CONTROL_POINT_COUNT"]:
            self.input_cp = control & 0x3F
        elif opcode == DCL["OUTPUT_CONTROL_POINT_COUNT"]:
            self.output_cp = control & 0x3F
        elif opcode == DCL["TESS_DOMAIN"]:
            self.domain = control & 3
        elif opcode == DCL["TESS_PARTITIONING"]:
            self.partitioning = control & 7
        elif opcode == DCL["TESS_OUTPUT_PRIMITIVE"]:
            self.output_primitive = control & 7
        elif opcode == DCL["HS_MAX_TESSFACTOR"]:
            self.max_tessfactor = struct.unpack("<f", struct.pack("<I", tokens[i + 1]))[0]
        elif opcode in (DCL["HS_FORK_PHASE_INSTANCE_COUNT"], DCL["HS_JOIN_PHASE_INSTANCE_COUNT"]):
            phase.instances = max(1, tokens[i + 1])
        elif opcode in (DCL["FUNCTION_BODY"], DCL["FUNCTION_TABLE"], DCL["INTERFACE"]):
            raise Unsupported("interfaces (dynamic shader linkage)")

    @staticmethod
    def _link(phase: Phase):
        """Matching else / endif / endloop / endswitch; the bodies of the labels."""
        program = phase.program
        stack = []
        for index, ins in enumerate(program):
            code = ins.opcode
            if code in (OPS["IF"], OPS["LOOP"], OPS["SWITCH"]):
                stack.append(index)
                if code == OPS["SWITCH"]:
                    ins.extra = []
            elif code == OPS["ELSE"] and stack:
                program[stack[-1]].jump = index
                stack[-1] = index
            elif code in (OPS["ENDIF"], OPS["ENDLOOP"], OPS["ENDSWITCH"]) and stack:
                opening = stack.pop()
                program[opening].jump = index
                ins.jump = opening
            elif code == OPS["CASE"]:
                switch = next((program[k] for k in reversed(stack) if program[k].opcode == OPS["SWITCH"]), None)
                if switch is not None:
                    switch.extra.append(int(ins.operands[0].imm.view(numpy.int32)[0]))
                    ins.jump = next(k for k in reversed(stack) if program[k].opcode == OPS["SWITCH"])
            elif code == OPS["DEFAULT"]:
                ins.jump = next((k for k in reversed(stack) if program[k].opcode == OPS["SWITCH"]), None)
        starts = [(index, ins.operands[0].slot) for index, ins in enumerate(program) if ins.opcode == OPS["LABEL"]]
        for k, (index, label) in enumerate(starts):
            end = starts[k + 1][0] if k + 1 < len(starts) else len(program)
            phase.labels[label] = (index + 1, end)
        phase.main_end = starts[0][0] if starts else len(program)

    def register_of(self, semantic: str, index: int, outputs=False) -> Optional[int]:
        for element in self.outputs if outputs else self.inputs:
            if element["name"] == semantic.upper() and element["index"] == index:
                return element["register"]
        return None

    def check(self):
        if self.error:
            raise Unsupported(self.error)


def _signed4(value: int) -> int:
    return value - 16 if value & 8 else value


# ------------------------------------------------------------------ what a run sees
class Bindings:
    """The resources of a stage: constant buffers slot -> (k, 4) uint32, shader resource views and
    unordered access views (gpu.View), samplers (gpu.Sampler)."""

    def __init__(self, cbs=None, srvs=None, samplers=None, uavs=None):
        self.cbs = cbs or {}
        self.srvs = srvs or {}
        self.samplers = samplers or {}
        self.uavs = uavs or {}


class Result:
    def __init__(self):
        self.outputs: Dict[int, numpy.ndarray] = {}
        self.alive = None
        self.depth = None
        self.events: List[tuple] = []           # geometry shader: ("emit", stream, lanes, outputs) / ("cut", stream, lanes)


def _f(a):
    return a.view(numpy.float32)


def _u(a):
    return numpy.ascontiguousarray(a, dtype=numpy.float32).view(numpy.uint32)


class Machine:
    """The interpreter of a phase on n lanes. io: inputs reg -> (n, 4); inputs2d reg -> (n, k, 4) (control
    points, the vertices of a geometry shader's primitive); vocp (n, k, 4) output control points read by
    a hull shader's fork / join phases; vpc reg -> (n, 4) patch constants; system: name -> (n, 4) or (n,)."""

    def __init__(self, shader: Shader, phase: Phase, n: int, inputs=None, inputs2d=None, vocp=None, vpc=None,
                 system=None, bindings: Optional[Bindings] = None, tgsm=None, groups=None):
        self.s = shader
        self.phase = phase
        self.n = n
        self.inputs = inputs or {}
        self.inputs2d = inputs2d or {}
        self.vocp = vocp or {}
        self.vpc = vpc or {}
        self.system = system or {}
        self.b = bindings or Bindings()
        self.tgsm = tgsm or {}                  # g# -> (groups, dwords) uint32
        self.groups = groups                     # the group of each lane (compute)
        self.temps: Dict[int, numpy.ndarray] = {}
        self.result = Result()
        self.outputs = self.result.outputs
        self.indexable = {k: numpy.zeros((n, size, 4), dtype=numpy.uint32) for k, (size, _) in phase.indexable.items()}
        self.active = numpy.ones(n, dtype=bool)
        self.dead = numpy.zeros(n, dtype=bool)
        self.discarded = numpy.zeros(n, dtype=bool)
        self.helper = numpy.zeros(n, dtype=bool)   # the helper lanes of the quads (no side effects)

    # ------------------------------------------------------------------ operands
    def _index(self, index):
        base, rel = index
        if rel is None:
            return base
        return base + self.read(rel, "i")[:, 0].astype(numpy.int64)

    def _system(self, name):
        value = self.system.get(name)
        if value is None:
            return numpy.zeros((self.n, 4), dtype=numpy.uint32)
        value = numpy.asarray(value)
        if value.ndim == 1:
            value = numpy.repeat(value.astype(numpy.uint32)[:, None], 4, axis=1)
        return value

    @staticmethod
    def _gather(array, index, n):
        """array (n, k, 4), index scalar or (n,): (n, 4)."""
        if isinstance(index, numpy.ndarray):
            return array[numpy.arange(n), numpy.clip(index, 0, array.shape[1] - 1)]
        return array[:, min(max(index, 0), array.shape[1] - 1)]

    def _raw(self, op: Operand) -> numpy.ndarray:
        n = self.n
        t = op.type
        if t == IMM32:
            return numpy.broadcast_to(op.imm, (n, 4))
        if t == TEMP:
            return self.temps.setdefault(op.indices[0][0], numpy.zeros((n, 4), dtype=numpy.uint32))
        if t in (INPUT, INPUT_CONTROL_POINT, OUTPUT_CONTROL_POINT):
            if len(op.indices) == 2 or t != INPUT:
                table = self.vocp if t == OUTPUT_CONTROL_POINT else self.inputs2d
                register = op.indices[-1][0]
                array = table.get(register)
                if array is None:
                    return numpy.zeros((n, 4), dtype=numpy.uint32)
                return self._gather(array, self._index(op.indices[0]), n)
            index = self._index(op.indices[0])
            if isinstance(index, numpy.ndarray):
                out = numpy.zeros((n, 4), dtype=numpy.uint32)
                for value in numpy.unique(index):
                    lanes = index == value
                    source = self.inputs.get(int(value))
                    if source is not None:
                        out[lanes] = source[lanes]
                return out
            value = self.inputs.get(index)
            return value if value is not None else numpy.zeros((n, 4), dtype=numpy.uint32)
        if t == INPUT_PATCH_CONSTANT:
            value = self.vpc.get(self._index(op.indices[0]) if not op.indices[0][1] else op.indices[0][0])
            return value if value is not None else numpy.zeros((n, 4), dtype=numpy.uint32)
        if t == OUTPUT:
            index = self._index(op.indices[-1])
            if isinstance(index, numpy.ndarray):
                out = numpy.zeros((n, 4), dtype=numpy.uint32)
                for value in numpy.unique(index):
                    lanes = index == value
                    out[lanes] = self.outputs.setdefault(int(value), numpy.zeros((n, 4), dtype=numpy.uint32))[lanes]
                return out
            return self.outputs.setdefault(index, numpy.zeros((n, 4), dtype=numpy.uint32))
        if t == INDEXABLE_TEMP:
            array = self.indexable[op.indices[0][0]]
            return self._gather(array, self._index(op.indices[1]), n)
        if t == CONSTANT_BUFFER:
            buffer = self.b.cbs.get(self._slot(op) if self.s.sm51 and len(op.indices) >= 3 else op.indices[0][0])
            if buffer is None or len(buffer) == 0:
                return numpy.zeros((n, 4), dtype=numpy.uint32)
            index = self._index(op.indices[-1])
            if isinstance(index, numpy.ndarray):
                return buffer[numpy.clip(index, 0, len(buffer) - 1)]
            return numpy.broadcast_to(buffer[min(max(index, 0), len(buffer) - 1)], (n, 4))
        if t == ICB:
            if self.s.icb is None:
                return numpy.zeros((n, 4), dtype=numpy.uint32)
            index = self._index(op.indices[0])
            if isinstance(index, numpy.ndarray):
                return self.s.icb[numpy.clip(index, 0, len(self.s.icb) - 1)]
            return numpy.broadcast_to(self.s.icb[min(max(index, 0), len(self.s.icb) - 1)], (n, 4))
        if t in SYSTEM_OPERANDS:
            return self._system(SYSTEM_OPERANDS[t])
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

    def bits(self, op: Operand) -> numpy.ndarray:
        """The raw 32-bit values of a source (mov, movc, stores)."""
        if op.modifier:
            return _u(self.read(op))
        return numpy.ascontiguousarray(self._raw(op)[:, list(op.swizzle)])

    def write(self, op: Operand, value: numpy.ndarray, saturate=False, lanes=None):
        if op.type == NULL:
            return
        if value.dtype in (numpy.float32, numpy.float64):
            value = value.astype(numpy.float32)
            if saturate:
                value = numpy.clip(numpy.nan_to_num(value, nan=0.0), 0.0, 1.0)
            value = value.view(numpy.uint32)
        elif value.dtype == numpy.bool_:
            value = numpy.where(value, 0xFFFFFFFF, 0).astype(numpy.uint32)
        elif value.dtype != numpy.uint32:
            value = (value.astype(numpy.int64) & 0xFFFFFFFF).astype(numpy.uint32)
        if value.ndim == 1:
            value = numpy.repeat(value[:, None], 4, axis=1)
        active = self.active if lanes is None else lanes
        if op.type in (OUTPUT_DEPTH, OUTPUT_DEPTH_GE, OUTPUT_DEPTH_LE):
            if self.result.depth is None:
                self.result.depth = numpy.zeros(self.n, dtype=numpy.float32)
            self.result.depth[active] = value[active, 0].view(numpy.float32)
            return
        if op.type in (OUTPUT_COVERAGE_MASK,):
            return
        if op.type == INDEXABLE_TEMP:
            array = self.indexable[op.indices[0][0]]
            index = self._index(op.indices[1])
            sel = numpy.nonzero(active)[0]
            rows = numpy.clip(index[sel] if isinstance(index, numpy.ndarray) else numpy.full(len(sel), index), 0,
                              array.shape[1] - 1)
            for c in range(4):
                if op.mask >> c & 1:
                    array[sel, rows, c] = value[sel, c]
            return
        if op.type == OUTPUT:
            index = self._index(op.indices[-1])
            if isinstance(index, numpy.ndarray):
                for register in numpy.unique(index[active]):
                    target = self.outputs.setdefault(int(register), numpy.zeros((self.n, 4), dtype=numpy.uint32))
                    lanes_r = active & (index == register)
                    for c in range(4):
                        if op.mask >> c & 1:
                            target[lanes_r, c] = value[lanes_r, c]
                return
        target = self._raw(op)
        if not target.flags.writeable:
            raise Unsupported(f"write to operand type {op.type}")
        mask = op.mask & 15
        if not mask:
            return
        low, high = (mask & -mask).bit_length() - 1, mask.bit_length()
        if mask == (1 << high) - (1 << low):    # contiguous components: one copy (all the lanes: no mask)
            part = slice(low, high)
            if active.all():
                target[:, part] = value[:, part]
            else:
                numpy.copyto(target[:, part], value[:, part], where=active[:, None])
            return
        for c in range(4):
            if mask >> c & 1:
                numpy.copyto(target[:, c], value[:, c], where=active)

    # ------------------------------------------------------------------ execution
    def run(self) -> Result:
        program = self.phase.program
        end = self.phase.main_end
        frames = []   # [kind, previous active, a, b, c]
        pc = 0
        steps = 0
        with numpy.errstate(all="ignore"):
            while pc < len(program):
                if pc == end and not any(f[0] == "call" for f in frames):
                    break
                ins = program[pc]
                code = ins.opcode
                steps += 1
                if steps > 1 << 24:
                    raise Unsupported("a shader that does not end")
                if code == OPS["IF"]:
                    condition = self._test(ins)
                    frames.append(["if", self.active.copy(), condition])
                    self.active = self.active & condition
                    if not self.active.any() and ins.jump is not None:
                        pc = ins.jump
                        continue
                elif code == OPS["ELSE"]:
                    frame = frames[-1]
                    self.active = frame[1] & ~frame[2] & ~self.dead & self._block_alive(frames[:-1])
                    if not self.active.any():
                        pc = ins.jump
                        continue
                elif code == OPS["ENDIF"]:
                    frame = frames.pop()
                    self.active = frame[1] & ~self.dead & self._block_alive(frames)
                elif code == OPS["LOOP"]:
                    frames.append(["loop", self.active.copy(), numpy.zeros(self.n, bool), numpy.zeros(self.n, bool), 0])
                elif code == OPS["ENDLOOP"]:
                    frame = frames[-1]
                    frame[3][:] = False
                    frame[4] += 1
                    self.active = frame[1] & ~frame[2] & ~self.dead & self._block_alive(frames[:-1])
                    if self.active.any() and frame[4] < MAX_LOOP:
                        pc = ins.jump + 1
                        continue
                    frames.pop()
                    self.active = frame[1] & ~self.dead & self._block_alive(frames)
                elif code == OPS["SWITCH"]:
                    value = self.read(ins.operands[0], "i")[:, 0]
                    cases = numpy.array(ins.extra or [], dtype=numpy.int64)
                    known = numpy.isin(value, cases)
                    frames.append(["switch", self.active.copy(), numpy.zeros(self.n, bool), value, known])
                    self.active = numpy.zeros(self.n, bool)
                elif code in (OPS["CASE"], OPS["DEFAULT"]):
                    frame = next(f for f in reversed(frames) if f[0] == "switch")
                    if code == OPS["CASE"]:
                        hit = frame[3] == int(ins.operands[0].imm.view(numpy.int32)[0])
                    else:
                        hit = ~frame[4]
                    self.active = (self.active | (frame[1] & hit)) & ~frame[2] & ~self.dead
                elif code == OPS["ENDSWITCH"]:
                    frame = frames.pop()
                    self.active = frame[1] & ~self.dead & self._block_alive(frames)
                elif code in (OPS["BREAK"], OPS["BREAKC"], OPS["CONTINUE"], OPS["CONTINUEC"]):
                    hit = self.active & (self._test(ins) if code in (OPS["BREAKC"], OPS["CONTINUEC"]) else True)
                    if code in (OPS["BREAK"], OPS["BREAKC"]):
                        target = next(f for f in reversed(frames) if f[0] in ("loop", "switch"))
                        target[2] |= hit
                    else:
                        target = next(f for f in reversed(frames) if f[0] == "loop")
                        target[3] |= hit
                    self.active = self.active & ~hit
                elif code in (OPS["CALL"], OPS["CALLC"]):
                    hit = self.active & (self._test(ins) if code == OPS["CALLC"] else True)
                    label = ins.operands[-1].slot
                    start, stop = self.phase.labels.get(label, (None, None))
                    if start is not None and hit.any():
                        frames.append(["call", self.active.copy(), numpy.zeros(self.n, bool), pc + 1, stop])
                        self.active = hit
                        pc = start
                        continue
                elif code in (OPS["RET"], OPS["RETC"]):
                    hit = self.active & (self._test(ins) if code == OPS["RETC"] else True)
                    call = next((f for f in reversed(frames) if f[0] == "call"), None)
                    if call is None:
                        self.dead |= hit
                    else:
                        call[2] |= hit
                    self.active = self.active & ~hit
                    if call is None and not frames and not self.active.any():
                        break
                elif code == OPS["LABEL"]:
                    pass
                elif code == OPS["DISCARD"]:
                    hit = self.active & self._test(ins)
                    self.discarded |= hit
                    self.dead |= hit
                    self.active = self.active & ~hit
                elif self.active.any():
                    self._compute(ins)
                pc += 1
                # the end of a subroutine's body
                while frames and frames[-1][0] == "call" and pc >= frames[-1][4]:
                    frame = frames.pop()
                    self.active = frame[1] & ~self.dead & self._block_alive(frames)
                    pc = frame[3]
        self.result.alive = ~self.discarded
        return self.result

    def _block_alive(self, frames):
        """The lanes not stopped by the enclosing loops / switches / calls (break, continue, ret)."""
        alive = numpy.ones(self.n, dtype=bool)
        for frame in frames:
            if frame[0] == "loop":
                alive &= ~frame[2] & ~frame[3]
            elif frame[0] == "switch":
                alive &= ~frame[2]
            elif frame[0] == "call":
                alive &= ~frame[2]
        return alive

    def _test(self, ins):
        value = self.read(ins.operands[0], "u")[:, 0]
        return value != 0 if ins.nonzero else value == 0

    # ------------------------------------------------------------------ instructions
    def _compute(self, ins: Instruction):
        code, ops, sat = ins.opcode, ins.operands, ins.saturate
        r = self.read
        handler = _HANDLERS.get(code)
        if handler is not None:
            handler(self, ins)
        elif code in _FLOAT2:
            self.write(ops[0], _FLOAT2[code](r(ops[1]), r(ops[2])), sat)
        elif code in _FLOAT1:
            self.write(ops[0], _FLOAT1[code](r(ops[1])), sat)
        elif code in _FCOMPARE:
            self.write(ops[0], _FCOMPARE[code](r(ops[1]), r(ops[2])))
        elif code in _ICOMPARE:
            kind = "u" if code in (OPS["ULT"], OPS["UGE"]) else "i"
            self.write(ops[0], _ICOMPARE[code](r(ops[1], kind), r(ops[2], kind)))
        elif code in _INT2:
            kind = "u" if code in (OPS["UMAX"], OPS["UMIN"], OPS["USHR"]) else "i"
            self.write(ops[0], _INT2[code](r(ops[1], kind), r(ops[2], kind)))
        else:
            raise Unsupported(f"instruction {NAMES.get(code, code)}")

    # resources ----------------------------------------------------------
    def _slot(self, op) -> int:
        """The register of a resource, sampler, UAV or constant buffer operand (5.1: its index in the
        range)."""
        if self.s.sm51 and len(op.indices) >= 2:
            index = self._index(op.indices[1])
            if isinstance(index, numpy.ndarray):
                return int(index[0]) if len(index) else 0
            return int(index)
        return op.indices[0][0] if op.indices else 0

    def _srv(self, op):
        return self.b.srvs.get(self._slot(op))

    def _sampler(self, op):
        from .gpu import DEFAULT_SAMPLER
        return self.b.samplers.get(self._slot(op)) or DEFAULT_SAMPLER

    def _coords(self, view, coord):
        """Texture coordinates for gpu.sample_view (the array slice stays an index)."""
        return coord.astype(numpy.float64)

    def sample(self, ins: Instruction):
        from .gpu import sample_view
        ops = ins.operands
        code = ins.opcode
        coord = self.read(ops[1]).astype(numpy.float64)
        view = self._srv(ops[2])
        sampler = self._sampler(ops[3])
        lod = None
        compare = None
        bias = None
        gradients = None
        if code == OPS["SAMPLE_L"]:
            lod = self.read(ops[4])[:, 0]
        elif code == OPS["SAMPLE_B"]:
            bias = self.read(ops[4])[:, 0].astype(numpy.float64)
        elif code == OPS["SAMPLE_D"]:
            gradients = (self.read(ops[4]).astype(numpy.float64), self.read(ops[5]).astype(numpy.float64))
        elif code in (OPS["SAMPLE_C"], OPS["SAMPLE_C_LZ"]):
            compare = self.read(ops[4])[:, 0]
            if code == OPS["SAMPLE_C_LZ"]:
                lod = numpy.zeros(self.n)
        value = sample_view(view, sampler, coord, lod, compare, ins.offsets[:2], bias, gradients) if view is not None \
            else numpy.zeros((self.n, 4), dtype=numpy.float32)
        self.write(ops[0], value[:, list(ops[2].swizzle)].astype(numpy.float32), ins.saturate)

    def gather(self, ins: Instruction):
        from .gpu import load_view
        ops = ins.operands
        code = ins.opcode
        offset_op = None
        if code in (OPS["GATHER4_PO"], OPS["GATHER4_PO_C"]):
            offset_op = ops[2]
            ops = [ops[0], ops[1]] + ops[3:]
        coord = self.read(ops[1]).astype(numpy.float64)
        view = self._srv(ops[2])
        channel = ops[3].swizzle[0] if ops[3].components == 2 else 0
        out = numpy.zeros((self.n, 4), dtype=numpy.float32)
        image = view.level(0, 0) if view is not None else None
        if image is not None:
            dx, dy = ins.offsets[:2]
            if offset_op is not None:
                offs = self.read(offset_op, "i")
                dx, dy = offs[:, 0], offs[:, 1]
            x = numpy.floor(coord[:, 0] * image.width - 0.5).astype(numpy.int64) + dx
            y = numpy.floor(coord[:, 1] * image.height - 0.5).astype(numpy.int64) + dy
            for k, (ox, oy) in enumerate(((0, 1), (1, 1), (1, 0), (0, 0))):
                xs = numpy.clip(x + ox, 0, image.width - 1)
                ys = numpy.clip(y + oy, 0, image.height - 1)
                out[:, k] = image.texels()[ys, xs, channel]
            if code in (OPS["GATHER4_C"], OPS["GATHER4_PO_C"]):
                reference = self.read(ops[4])[:, 0]
                out = (reference[:, None] <= out).astype(numpy.float32)
        self.write(ops[0], out[:, list(ops[2].swizzle)], ins.saturate)

    def load(self, ins: Instruction):
        from .gpu import load_view
        ops = ins.operands
        coord = self.read(ops[1], "i")
        view = self._srv(ops[2]) if ops[2].type == RESOURCE else self.b.uavs.get(self._slot(ops[2]))
        # the mip level is in w (the texture's views); ld_uav_typed and buffers have none
        mip_level = int(numpy.median(coord[:, 3])) if view is not None and view.kind == "texture" and             ins.opcode in (OPS["LD"], OPS["LD_MS"]) and len(coord) else 0
        value = load_view(view, coord, mip_level)
        self.write(ops[0], self._typed(view, value)[:, list(ops[2].swizzle)], ins.saturate)

    @staticmethod
    def _typed(view, value):
        """Loaded values as register bits: integers of uint / sint formats, floats otherwise."""
        from .gpu import is_integer
        if view is not None and view.kind == "buffer" and is_integer(view.fmt) or \
                view is not None and view.images and view.level(0, 0) is not None and is_integer(view.level(0, 0).kind):
            return numpy.round(value).astype(numpy.int64)
        return value.astype(numpy.float32)

    def resinfo(self, ins: Instruction):
        ops = ins.operands
        mip = self.read(ops[1], "u")[:, 0]
        view = self._srv(ops[2]) if ops[2].type == RESOURCE else self.b.uavs.get(self._slot(ops[2]))
        out = numpy.zeros((self.n, 4), dtype=numpy.float64)
        if view is not None and view.images:
            level = int(mip[0]) if len(mip) else 0
            image = view.level(0, level)
            if image is not None:
                layers = len(view.images)
                out[:] = [image.width, image.height if view.dimension not in ("1d", "1darray") else layers,
                          layers if view.dimension in ("2darray", "cube", "cubearray") else 1, view.mips]
        kind = ins.control & 3
        if kind == 2:
            self.write(ops[0], out[:, list(ops[2].swizzle)].astype(numpy.int64))
        else:
            if kind == 1:
                out[:, :3] = numpy.where(out[:, :3] != 0, 1 / numpy.where(out[:, :3] == 0, 1, out[:, :3]), 0)
            self.write(ops[0], out[:, list(ops[2].swizzle)].astype(numpy.float32), ins.saturate)

    def bufinfo(self, ins: Instruction):
        ops = ins.operands
        view = self._srv(ops[1]) if ops[1].type == RESOURCE else self.b.uavs.get(self._slot(ops[1]))
        count = 0
        if view is not None:
            if view.kind == "raw":
                count = view.count * 4 if view.count else (len(view.bytes) if view.bytes is not None else 0)
            else:
                count = view.count
        self.write(ops[0], numpy.full(self.n, count, dtype=numpy.int64))

    # memory: raw / structured buffers, UAVs, group shared memory ----------
    def _memory(self, op: Operand):
        """(dwords array (uint32 view), per-lane base dword offset or None, stride) of a buffer or g#."""
        if op.type == TGSM:
            memory = self.tgsm.get(op.slot)
            stride = self.s.tgsm.get(op.slot, (0, 0))[1]
            return memory, stride, True
        view = self._srv(op) if op.type == RESOURCE else self.b.uavs.get(self._slot(op))
        if view is None or view.bytes is None:
            return None, 0, False
        return view, view.stride, False

    def _addresses(self, op, index_op, offset_op):
        """The byte address of each lane in the memory of op."""
        if index_op is None:
            return self.read(offset_op, "u")[:, 0].astype(numpy.int64)
        index = self.read(index_op, "u")[:, 0].astype(numpy.int64)
        offset = self.read(offset_op, "u")[:, 0].astype(numpy.int64)
        _, stride, _ = self._memory(op)
        return index * stride + offset

    def _dwords(self, op):
        """A writable uint32 view of the memory of op, and per lane: the row (group) for g#."""
        memory, _, shared = self._memory(op)
        if memory is None:
            return None, None
        if shared:
            return memory, self.groups
        view = memory
        base = view.first * (view.stride or 4) if view.kind in ("raw", "structured") else 0
        data = view.bytes
        if base % 4 or len(data) % 4:
            data = view.bytes  # misaligned: rare, handled below as bytes
        dwords = data[:len(data) - len(data) % 4].view("<u4")
        if base:
            dwords = dwords[base // 4:]
        view.dirty = True
        return dwords, None

    def ld_raw(self, ins: Instruction, structured=False):
        ops = ins.operands
        source = ops[3] if structured else ops[2]
        address = self._addresses(source, ops[1] if structured else None, ops[2] if structured else ops[1])
        memory, rows = self._dwords(source)
        out = numpy.zeros((self.n, 4), dtype=numpy.uint32)
        if memory is not None:
            dword = address // 4
            for c in range(4):
                index = dword + c
                if rows is None:
                    ok = (index >= 0) & (index < len(memory))
                    out[ok, c] = memory[index[ok]]
                else:
                    ok = (index >= 0) & (index < memory.shape[1])
                    out[ok, c] = memory[rows[ok], index[ok]]
        self.write(ops[0], out[:, list(source.swizzle)])

    def store_raw(self, ins: Instruction, structured=False):
        ops = ins.operands
        target = ops[0]
        address = self._addresses(target, ops[1] if structured else None, ops[2] if structured else ops[1])
        value = self.bits(ops[3] if structured else ops[2])
        memory, rows = self._dwords(target)
        if memory is None:
            return
        lanes = numpy.nonzero(self.active & ~self.helper)[0]
        dword = address // 4
        written = 0
        for c in range(4):
            if not target.mask >> c & 1:
                continue
            index = dword[lanes] + written
            src = value[lanes, written]
            written += 1
            if rows is None:
                ok = (index >= 0) & (index < len(memory))
                memory[index[ok]] = src[ok]
            else:
                ok = (index >= 0) & (index < memory.shape[1])
                memory[rows[lanes][ok], index[ok]] = src[ok]

    def ld_typed(self, ins: Instruction):
        from .gpu import load_view
        ops = ins.operands
        view = self.b.uavs.get(self._slot(ops[2]))
        coord = self.read(ops[1], "i")
        value = load_view(view, coord, 0)
        self.write(ops[0], self._typed(view, value)[:, list(ops[2].swizzle)])

    def store_typed(self, ins: Instruction):
        from .gpu import store_view, is_integer
        ops = ins.operands
        view = self.b.uavs.get(self._slot(ops[0]))
        if view is None:
            return
        coord = self.read(ops[1], "i")
        integer = view.kind == "buffer" and is_integer(view.fmt) or \
            view.images and view.level(0, 0) is not None and is_integer(view.level(0, 0).kind)
        values = self.read(ops[2], "u").astype(numpy.float64) if integer else self.read(ops[2]).astype(numpy.float64)
        store_view(view, coord, values, numpy.nonzero(self.active & ~self.helper)[0])
        view.dirty = True

    def atomic(self, ins: Instruction):
        code = ins.opcode
        ops = ins.operands
        immediate = code >= OPS["IMM_ATOMIC_ALLOC"]
        if code in (OPS["IMM_ATOMIC_ALLOC"], OPS["IMM_ATOMIC_CONSUME"]):
            view = self.b.uavs.get(self._slot(ops[1]))
            lanes = numpy.nonzero(self.active & ~self.helper)[0]
            counter = view.counter if view is not None and view.counter is not None else [0]
            out = numpy.zeros(self.n, dtype=numpy.int64)
            for lane in lanes:
                if code == OPS["IMM_ATOMIC_ALLOC"]:
                    out[lane] = counter[0]
                    counter[0] += 1
                else:
                    counter[0] -= 1
                    out[lane] = counter[0]
            if view is not None:
                view.counter = counter
            self.write(ops[0], out)
            return
        target = ops[1] if immediate else ops[0]
        address_op = ops[2] if immediate else ops[1]
        values = ops[3:] if immediate else ops[2:]
        view = None if target.type == TGSM else (self._srv(target) if target.type == RESOURCE else self.b.uavs.get(self._slot(target)))
        lanes = numpy.nonzero(self.active & ~self.helper)[0]
        operand_values = [self.read(v, "u")[:, 0].astype(numpy.int64) for v in values]
        if view is not None and view.kind in ("texture", "buffer"):
            memory, index, rows = self._typed_cells(view, address_op)
        else:
            memory, rows = self._dwords(target)
            if memory is None:
                return
            if target.type == TGSM or view is None:
                index = self.read(address_op, "u")[:, 0].astype(numpy.int64) // 4
                if target.type == TGSM and self.s.tgsm.get(target.slot, (0, 0))[1]:
                    address = self.read(address_op, "u")
                    index = (address[:, 0].astype(numpy.int64) * self.s.tgsm[target.slot][1] + address[:, 1]) // 4
            else:
                address = self.read(address_op, "u")
                if view.kind == "structured":
                    index = (address[:, 0].astype(numpy.int64) * view.stride + address[:, 1]) // 4
                else:
                    index = address[:, 0].astype(numpy.int64) // 4
        out = numpy.zeros(self.n, dtype=numpy.int64)
        name = NAMES[code].replace("IMM_ATOMIC_", "").replace("ATOMIC_", "")
        for lane in lanes:
            i = index[lane]
            row = rows[lane] if rows is not None else None
            try:
                old = int(memory[row, i] if row is not None else memory[i])
            except IndexError:
                continue
            a = operand_values[0][lane]
            new = old
            if name == "IADD":
                new = (old + a) & 0xFFFFFFFF
            elif name == "AND":
                new = old & a
            elif name == "OR":
                new = old | a
            elif name == "XOR":
                new = old ^ a
            elif name == "EXCH":
                new = a
            elif name in ("CMP_STORE", "CMP_EXCH"):
                new = operand_values[1][lane] if old == a else old
            elif name in ("IMAX", "IMIN"):
                so, sa = _s32(old), _s32(a)
                new = (max(so, sa) if name == "IMAX" else min(so, sa)) & 0xFFFFFFFF
            elif name == "UMAX":
                new = max(old, a)
            elif name == "UMIN":
                new = min(old, a)
            if row is not None:
                memory[row, i] = new
            else:
                memory[i] = new
            out[lane] = old
        if view is not None and view.kind in ("texture", "buffer"):
            self._typed_writeback(view, memory)
        if immediate:
            self.write(ops[0], out)

    def _typed_cells(self, view, address_op):
        """Atomics on a typed view (R32_UINT / SINT): its values as a flat array, an index per lane."""
        coord = self.read(address_op, "i").astype(numpy.int64)
        if view.kind == "buffer":
            data = view.bytes[:len(view.bytes) // 4 * 4].view("<u4")
            view.dirty = True
            return data, coord[:, 0] + view.first, None
        image = view.level(0, 0)
        values = numpy.round(image.texels()[..., 0]).astype(numpy.int64).reshape(-1) & 0xFFFFFFFF
        view._atomic_image = image
        return values, coord[:, 1] * image.width + coord[:, 0], None

    @staticmethod
    def _typed_writeback(view, memory):
        image = getattr(view, "_atomic_image", None)
        if image is not None:
            texels = image.texels().copy()
            texels[..., 0] = numpy.asarray(memory, dtype=numpy.float64).reshape(image.height, image.width)
            image.set_texels(texels)
            view._atomic_image = None

    # geometry shader ------------------------------------------------------
    def emit(self, ins: Instruction):
        code = ins.opcode
        stream = ins.operands[0].slot if code in (OPS["EMIT_STREAM"], OPS["CUT_STREAM"], OPS["EMITTHENCUT_STREAM"]) else 0
        lanes = numpy.nonzero(self.active)[0]
        if code in (OPS["EMIT"], OPS["EMITTHENCUT"], OPS["EMIT_STREAM"], OPS["EMITTHENCUT_STREAM"]):
            snapshot = {k: v[lanes].copy() for k, v in self.outputs.items()}
            self.result.events.append(("emit", stream, lanes, snapshot))
        if code in (OPS["CUT"], OPS["EMITTHENCUT"], OPS["CUT_STREAM"], OPS["EMITTHENCUT_STREAM"]):
            self.result.events.append(("cut", stream, lanes, None))


def _s32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value >> 31 else value


# ------------------------------------------------------------------ the arithmetic
def _mad(m, ins):
    o = ins.operands
    m.write(o[0], m.read(o[1]) * m.read(o[2]) + m.read(o[3]), ins.saturate)


def _dp(width):
    def run(m, ins):
        o = ins.operands
        m.write(o[0], (m.read(o[1])[:, :width] * m.read(o[2])[:, :width]).sum(axis=1), ins.saturate)
    return run


def _mov(m, ins):
    o = ins.operands
    if ins.saturate:
        m.write(o[0], m.read(o[1]), True)
    else:
        m.write(o[0], m.bits(o[1]))


def _movc(m, ins):
    o = ins.operands
    condition = m.read(o[1], "u") != 0
    value = numpy.where(condition, m.bits(o[2]), m.bits(o[3])).astype(numpy.uint32)
    m.write(o[0], value.view(numpy.float32).copy() if ins.saturate else value, ins.saturate)


def _swapc(m, ins):
    o = ins.operands
    condition = m.read(o[2], "u") != 0
    a, b = m.bits(o[3]), m.bits(o[4])
    m.write(o[0], numpy.where(condition, b, a).astype(numpy.uint32))
    m.write(o[1], numpy.where(condition, a, b).astype(numpy.uint32))


def _sincos(m, ins):
    o = ins.operands
    x = m.read(o[2])
    m.write(o[0], numpy.sin(x), ins.saturate)
    m.write(o[1], numpy.cos(x), ins.saturate)


def _mul_wide(signed):
    def run(m, ins):
        o = ins.operands
        if signed:
            product = m.read(o[2], "i").astype(numpy.int64) * m.read(o[3], "i").astype(numpy.int64)
            high = (product >> 32) & 0xFFFFFFFF
        else:
            product = m.read(o[2], "u").astype(numpy.uint64) * m.read(o[3], "u").astype(numpy.uint64)
            high = product >> numpy.uint64(32)
        m.write(o[0], high.astype(numpy.uint64).astype(numpy.uint32))
        m.write(o[1], (product & 0xFFFFFFFF).astype(numpy.uint64).astype(numpy.uint32))
    return run


def _mad_int(signed):
    def run(m, ins):
        o = ins.operands
        kind = "i" if signed else "u"
        value = m.read(o[1], kind).astype(numpy.int64) * m.read(o[2], kind).astype(numpy.int64) + \
            m.read(o[3], kind).astype(numpy.int64)
        m.write(o[0], value & 0xFFFFFFFF)
    return run


def _udiv(m, ins):
    o = ins.operands
    a, b = m.read(o[2], "u"), m.read(o[3], "u")
    safe = numpy.where(b == 0, 1, b)
    m.write(o[0], numpy.where(b == 0, 0xFFFFFFFF, a // safe).astype(numpy.uint32))
    m.write(o[1], numpy.where(b == 0, 0xFFFFFFFF, a % safe).astype(numpy.uint32))


def _uaddc(subtract):
    def run(m, ins):
        o = ins.operands
        a, b = m.read(o[2], "u").astype(numpy.int64), m.read(o[3], "u").astype(numpy.int64)
        value = a - b if subtract else a + b
        m.write(o[0], value & 0xFFFFFFFF)
        m.write(o[1], (value < 0).astype(numpy.int64) if subtract else (value >> 32) & 1)
    return run


def _unary_int(function, kind="u"):
    def run(m, ins):
        o = ins.operands
        m.write(o[0], function(m.read(o[1], kind)))
    return run


def _convert(kind, target):
    def run(m, ins):
        o = ins.operands
        value = m.read(o[1], kind) if kind != "f" else m.read(o[1])
        if target == "f":
            m.write(o[0], value.astype(numpy.float32), ins.saturate)
        elif target == "i":
            m.write(o[0], numpy.clip(numpy.nan_to_num(numpy.trunc(value)), -2 ** 31, 2 ** 31 - 1).astype(numpy.int64))
        else:
            m.write(o[0], numpy.clip(numpy.nan_to_num(numpy.trunc(value)), 0, 2 ** 32 - 1).astype(numpy.uint64).astype(numpy.int64))
    return run


def _f32tof16(m, ins):
    o = ins.operands
    m.write(o[0], m.read(o[1]).astype(numpy.float16).view(numpy.uint16).astype(numpy.int64))


def _f16tof32(m, ins):
    o = ins.operands
    m.write(o[0], (m.read(o[1], "u") & 0xFFFF).astype(numpy.uint16).view(numpy.float16).astype(numpy.float32), ins.saturate)


def _countbits(value):
    value = value.astype(numpy.uint64)
    count = numpy.zeros(value.shape, dtype=numpy.int64)
    for _ in range(32):
        count += (value & numpy.uint64(1)).astype(numpy.int64)
        value >>= numpy.uint64(1)
    return count


def _firstbit_lo(value):
    value = value.astype(numpy.int64)
    out = numpy.full(value.shape, -1, dtype=numpy.int64)
    for bit in range(31, -1, -1):
        out = numpy.where((value >> bit) & 1 == 1, bit, out)
    return out


def _firstbit_hi(value):
    value = value.astype(numpy.int64) & 0xFFFFFFFF
    out = numpy.full(value.shape, -1, dtype=numpy.int64)
    for bit in range(32):
        out = numpy.where((value >> bit) & 1 == 1, 31 - bit, out)
    return out


def _firstbit_shi(value):
    signed = value.view(numpy.int32).astype(numpy.int64)
    flipped = numpy.where(signed < 0, ~signed, signed) & 0xFFFFFFFF
    return _firstbit_hi(flipped)


def _bitfield(signed):
    def run(m, ins):
        o = ins.operands
        width = m.read(o[1], "u").astype(numpy.int64) & 31
        offset = m.read(o[2], "u").astype(numpy.int64) & 31
        value = m.read(o[3], "u").astype(numpy.int64)
        field = (value >> offset) & ((1 << width) - 1)
        if signed:
            sign = numpy.where(width > 0, (field >> numpy.maximum(width - 1, 0)) & 1, 0)
            field = numpy.where(sign == 1, field - (1 << width), field)
        result = numpy.where(width == 0, 0, numpy.where(width + offset < 32, field,
                                                         (value.view(numpy.int64) >> offset) if signed else value >> offset))
        if signed:
            raw = m.read(o[3], "i").astype(numpy.int64)
            result = numpy.where(width == 0, 0, numpy.where(width + offset < 32, field, raw >> offset))
        m.write(o[0], result & 0xFFFFFFFF)
    return run


def _bfi(m, ins):
    o = ins.operands
    width = m.read(o[1], "u").astype(numpy.int64) & 31
    offset = m.read(o[2], "u").astype(numpy.int64) & 31
    insert, base = m.read(o[3], "u").astype(numpy.int64), m.read(o[4], "u").astype(numpy.int64)
    mask = (((1 << width) - 1) << offset) & 0xFFFFFFFF
    m.write(o[0], ((insert << offset) & mask) | (base & ~mask & 0xFFFFFFFF))


def _bfrev(value):
    value = value.astype(numpy.int64)
    out = numpy.zeros(value.shape, dtype=numpy.int64)
    for bit in range(32):
        out |= ((value >> bit) & 1) << (31 - bit)
    return out


def _deriv(m, ins):
    from .raster import derivative
    o = ins.operands
    code = ins.opcode
    axis = "x" if code in (OPS["DERIV_RTX"], OPS["DERIV_RTX_COARSE"], OPS["DERIV_RTX_FINE"]) else "y"
    fine = code in (OPS["DERIV_RTX_FINE"], OPS["DERIV_RTY_FINE"], OPS["DERIV_RTX"], OPS["DERIV_RTY"])
    value = m.read(o[1]).astype(numpy.float64)
    m.write(o[0], derivative(value, axis, fine).astype(numpy.float32), ins.saturate)


def _eval(m, ins):
    o = ins.operands
    m.write(o[0], m.bits(o[1]))


def _lod(m, ins):
    """lod dest, coords, t#, s#: (clamped level, unclamped level, 0, 0) from the quads' derivatives."""
    o = ins.operands
    out = numpy.zeros((m.n, 4), dtype=numpy.float32)
    if ins.opcode == OPS["LOD"] and len(o) >= 4:
        from .gpu import view_lod, view_levels, Sampler
        view = m._srv(o[2])
        sampler = m._sampler(o[3])
        if view is not None and view.images:
            raw = view_lod(view, Sampler(sampler.linear, sampler.address, mip_linear=sampler.mip_linear),
                           m.read(o[1]).astype(numpy.float64))
            if raw is not None:
                count = len(view_levels(view, 0))
                clamped = numpy.clip(raw + sampler.lod_bias, max(sampler.min_lod, 0), min(sampler.max_lod, count - 1))
                if not sampler.mip_linear:
                    clamped = numpy.floor(clamped + 0.5)
                out[:, 0], out[:, 1] = clamped, raw
        out = out[:, list(o[2].swizzle)]
    m.write(o[0], out)


def _sample_info(m, ins):
    o = ins.operands
    m.write(o[0], numpy.ones((m.n, 4), dtype=numpy.float32) if not ins.control & 1 else numpy.ones(m.n, dtype=numpy.int64))


def _nothing(m, ins):
    pass


_HANDLERS = {
    OPS["MAD"]: _mad, OPS["DP2"]: _dp(2), OPS["DP3"]: _dp(3), OPS["DP4"]: _dp(4), OPS["MOV"]: _mov, OPS["MOVC"]: _movc,
    OPS["SWAPC"]: _swapc, OPS["SINCOS"]: _sincos, OPS["IMUL"]: _mul_wide(True), OPS["UMUL"]: _mul_wide(False),
    OPS["IMAD"]: _mad_int(True), OPS["UMAD"]: _mad_int(False), OPS["UDIV"]: _udiv, OPS["UADDC"]: _uaddc(False),
    OPS["USUBB"]: _uaddc(True), OPS["NOT"]: _unary_int(lambda v: ~v.astype(numpy.int64)),
    OPS["INEG"]: _unary_int(lambda v: -v.astype(numpy.int64), "i"), OPS["ITOF"]: _convert("i", "f"),
    OPS["UTOF"]: _convert("u", "f"), OPS["FTOI"]: _convert("f", "i"), OPS["FTOU"]: _convert("f", "u"),
    OPS["F32TOF16"]: _f32tof16, OPS["F16TOF32"]: _f16tof32, OPS["COUNTBITS"]: _unary_int(_countbits),
    OPS["FIRSTBIT_HI"]: _unary_int(_firstbit_hi), OPS["FIRSTBIT_LO"]: _unary_int(_firstbit_lo),
    OPS["FIRSTBIT_SHI"]: _unary_int(_firstbit_shi), OPS["UBFE"]: _bitfield(False), OPS["IBFE"]: _bitfield(True),
    OPS["BFI"]: _bfi, OPS["BFREV"]: _unary_int(_bfrev), OPS["DERIV_RTX"]: _deriv, OPS["DERIV_RTY"]: _deriv,
    OPS["DERIV_RTX_COARSE"]: _deriv, OPS["DERIV_RTX_FINE"]: _deriv, OPS["DERIV_RTY_COARSE"]: _deriv,
    OPS["DERIV_RTY_FINE"]: _deriv, OPS["SAMPLE"]: Machine.sample, OPS["SAMPLE_L"]: Machine.sample,
    OPS["SAMPLE_B"]: Machine.sample, OPS["SAMPLE_D"]: Machine.sample, OPS["SAMPLE_C"]: Machine.sample,
    OPS["SAMPLE_C_LZ"]: Machine.sample, OPS["GATHER4"]: Machine.gather, OPS["GATHER4_C"]: Machine.gather,
    OPS["GATHER4_PO"]: Machine.gather, OPS["GATHER4_PO_C"]: Machine.gather, OPS["LD"]: Machine.load,
    OPS["LD_MS"]: Machine.load, OPS["RESINFO"]: Machine.resinfo, OPS["BUFINFO"]: Machine.bufinfo,
    OPS["LD_RAW"]: Machine.ld_raw, OPS["STORE_RAW"]: Machine.store_raw,
    OPS["LD_STRUCTURED"]: lambda m, ins: Machine.ld_raw(m, ins, True),
    OPS["STORE_STRUCTURED"]: lambda m, ins: Machine.store_raw(m, ins, True),
    OPS["LD_UAV_TYPED"]: Machine.ld_typed, OPS["STORE_UAV_TYPED"]: Machine.store_typed,
    OPS["EMIT"]: Machine.emit, OPS["CUT"]: Machine.emit, OPS["EMITTHENCUT"]: Machine.emit,
    OPS["EMIT_STREAM"]: Machine.emit, OPS["CUT_STREAM"]: Machine.emit, OPS["EMITTHENCUT_STREAM"]: Machine.emit,
    OPS["SYNC"]: _nothing, OPS["NOP"]: _nothing, OPS["EVAL_SNAPPED"]: _eval, OPS["EVAL_SAMPLE_INDEX"]: _eval,
    OPS["EVAL_CENTROID"]: _eval, OPS["LOD"]: _lod, OPS["SAMPLE_INFO"]: _sample_info, OPS["SAMPLE_POS"]: _lod,
    OPS["ABORT"]: _nothing, OPS["DEBUG_BREAK"]: _nothing,
}
for _name in ("ATOMIC_AND", "ATOMIC_OR", "ATOMIC_XOR", "ATOMIC_CMP_STORE", "ATOMIC_IADD", "ATOMIC_IMAX", "ATOMIC_IMIN",
              "ATOMIC_UMAX", "ATOMIC_UMIN", "IMM_ATOMIC_ALLOC", "IMM_ATOMIC_CONSUME", "IMM_ATOMIC_IADD", "IMM_ATOMIC_AND",
              "IMM_ATOMIC_OR", "IMM_ATOMIC_XOR", "IMM_ATOMIC_EXCH", "IMM_ATOMIC_CMP_EXCH", "IMM_ATOMIC_IMAX",
              "IMM_ATOMIC_IMIN", "IMM_ATOMIC_UMAX", "IMM_ATOMIC_UMIN"):
    _HANDLERS[OPS[_name]] = Machine.atomic

_FLOAT2 = {OPS["ADD"]: numpy.add, OPS["MUL"]: numpy.multiply, OPS["DIV"]: numpy.divide, OPS["MIN"]: numpy.fmin,
           OPS["MAX"]: numpy.fmax}
_FLOAT1 = {OPS["FRC"]: lambda x: x - numpy.floor(x), OPS["ROUND_NE"]: numpy.round, OPS["ROUND_NI"]: numpy.floor,
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
_CONTROL = {OPS[k] for k in ("IF", "ELSE", "ENDIF", "LOOP", "ENDLOOP", "BREAK", "BREAKC", "CONTINUE", "CONTINUEC",
                             "RET", "RETC", "DISCARD", "SWITCH", "CASE", "DEFAULT", "ENDSWITCH", "CALL", "CALLC", "LABEL")}
_SUPPORTED = set(_HANDLERS) | set(_FLOAT2) | set(_FLOAT1) | set(_FCOMPARE) | set(_ICOMPARE) | set(_INT2) | _CONTROL


# ------------------------------------------------------------------ the stages
def run_vertex(shader: Shader, n: int, inputs, bindings: Bindings, system=None) -> Result:
    if getattr(shader, "dxil", False):
        from . import dxil
        return dxil.run_vertex(shader, n, inputs, bindings, system)
    shader.check()
    return Machine(shader, shader.main, n, inputs=inputs, system=system, bindings=bindings).run()


def run_pixel(shader: Shader, n: int, inputs, bindings: Bindings, system=None, helper=None) -> Result:
    if getattr(shader, "dxil", False):
        from . import dxil
        return dxil.run_pixel(shader, n, inputs, bindings, system, helper)
    shader.check()
    machine = Machine(shader, shader.main, n, inputs=inputs, system=system, bindings=bindings)
    if helper is not None:
        machine.helper = numpy.asarray(helper, dtype=bool)
    return machine.run()


def needs_quads(shader: Shader, mipmaps: bool = True) -> bool:
    """Whether a pixel shader takes derivatives: ddx / ddy, or (when a bound texture has mip levels) the
    mip level of sample, sample_b, sample_c, lod."""
    if getattr(shader, "dxil", False):
        from . import dxil
        return dxil.needs_quads(shader, mipmaps)
    names = ["DERIV_RTX", "DERIV_RTY", "DERIV_RTX_COARSE", "DERIV_RTX_FINE", "DERIV_RTY_COARSE", "DERIV_RTY_FINE"]
    if mipmaps:
        names += ["SAMPLE", "SAMPLE_B", "SAMPLE_C", "LOD"]
    codes = {OPS[name] for name in names}
    return any(ins.opcode in codes for phase in shader.phases for ins in phase.program)


GS_INPUT_VERTICES = {1: 1, 2: 2, 3: 3, 6: 4, 7: 6}


def run_geometry(shader: Shader, n: int, inputs2d, bindings: Bindings, primitive_id) -> List[Result]:
    """One run per instance (dcl_gs_instance_count): the events (emit / cut) of each."""
    if getattr(shader, "dxil", False):
        from . import dxil
        return dxil.run_geometry(shader, n, inputs2d, bindings, primitive_id)
    shader.check()
    results = []
    for instance in range(shader.gs_instances):
        system = {"primitive_id": primitive_id, "gs_instance": numpy.full(n, instance, dtype=numpy.uint32)}
        results.append(Machine(shader, shader.main, n, inputs2d=inputs2d, system=system, bindings=bindings).run())
    return results


def run_hull(shader: Shader, patches: int, vicp, bindings: Bindings, primitive_id):
    """The phases of a hull shader on patches: (output control points reg -> (p, k, 4), patch constants
    reg -> (p, 4))."""
    if getattr(shader, "dxil", False):
        from . import dxil
        return dxil.run_hull(shader, patches, vicp, bindings, primitive_id)
    shader.check()
    count = shader.output_cp or shader.input_cp or 1
    control = [p for p in shader.phases if p.kind == "control"]
    if control:
        lanes = patches * count
        patch_of = numpy.repeat(numpy.arange(patches), count)
        inputs2d = {reg: values[patch_of] for reg, values in vicp.items()}
        system = {"output_cp_id": numpy.tile(numpy.arange(count), patches).astype(numpy.uint32),
                  "primitive_id": primitive_id[patch_of]}
        result = Machine(shader, control[0], lanes, inputs2d=inputs2d, system=system, bindings=bindings).run()
        vocp = {reg: values.reshape(patches, count, 4) for reg, values in result.outputs.items()}
    else:
        vocp = {reg: values[:, :count] for reg, values in vicp.items()}
    constants: Dict[int, numpy.ndarray] = {}
    for phase in shader.phases:
        if phase.kind not in ("fork", "join"):
            continue
        for instance in range(phase.instances):
            system = {"primitive_id": primitive_id,
                      "fork_instance" if phase.kind == "fork" else "join_instance": numpy.full(patches, instance, numpy.uint32)}
            machine = Machine(shader, phase, patches, inputs2d=vicp, vocp=vocp, vpc=constants, system=system,
                              bindings=bindings)
            machine.outputs.update({k: v.copy() for k, v in constants.items()})
            result = machine.run()
            constants.update(result.outputs)
    return vocp, constants


def run_domain(shader: Shader, n: int, domain, vicp, vpc, bindings: Bindings, primitive_id) -> Result:
    if getattr(shader, "dxil", False):
        from . import dxil
        return dxil.run_domain(shader, n, domain, vicp, vpc, bindings, primitive_id)
    shader.check()
    system = {"domain": domain, "primitive_id": primitive_id}
    return Machine(shader, shader.main, n, inputs2d=vicp, vpc=vpc, system=system, bindings=bindings).run()


def run_compute(shader: Shader, groups, bindings: Bindings, limit=1 << 22):
    """A dispatch of groups (x, y, z): the threads of several groups at once (lanes), groups in order."""
    if getattr(shader, "dxil", False):
        from . import dxil
        return dxil.run_compute(shader, groups, bindings)
    shader.check()
    tx, ty, tz = (max(1, v) for v in shader.thread_group)
    per_group = tx * ty * tz
    gx, gy, gz = (max(0, int(v)) for v in groups)
    total = gx * gy * gz
    if total == 0:
        return
    batch = max(1, limit // per_group)
    local = numpy.arange(per_group)
    lx, ly, lz = local % tx, (local // tx) % ty, local // (tx * ty)
    for first in range(0, total, batch):
        ids = numpy.arange(first, min(total, first + batch))
        count = len(ids)
        n = count * per_group
        group = numpy.repeat(ids, per_group)
        g = numpy.stack([group % gx, (group // gx) % gy, group // (gx * gy)], 1)
        t = numpy.stack([numpy.tile(lx, count), numpy.tile(ly, count), numpy.tile(lz, count)], 1)
        dispatch = g * [tx, ty, tz] + t
        system = {"thread_id": _vec(dispatch), "group_id": _vec(g), "thread_in_group": _vec(t),
                  "flattened": numpy.tile(local, count).astype(numpy.uint32)}
        tgsm = {slot: numpy.zeros((count, max(1, size // 4)), dtype=numpy.uint32) for slot, (size, _) in shader.tgsm.items()}
        Machine(shader, shader.main, n, system=system, bindings=bindings, tgsm=tgsm,
                groups=numpy.repeat(numpy.arange(count), per_group)).run()


def _vec(values):
    out = numpy.zeros((len(values), 4), dtype=numpy.uint32)
    out[:, :values.shape[1]] = values
    return out


# ------------------------------------------------------------------ the tessellator
def tessellate(domain: int, factors, inside, partitioning: int = 1, output: int = 3):
    """The points (k, 3) in the domain (barycentric u, v, w for triangles, u, v for quads and lines) and the
    primitives (indices) of a patch, as the fixed tessellator of Direct3D 11 makes them (tessellator.py, a port of
    Microsoft's reference: the same points and triangles as a GPU). domain: 1 isoline, 2 triangle, 3 quad;
    partitioning: 1 integer, 2 pow2, 3 fractional_odd, 4 fractional_even; output: 1 points, 2 lines, 3
    triangles in the reference's (clockwise) order (the caller swaps them for triangle_ccw). Isolines:
    factors[0] the number of lines, factors[1] the segments of each. A factor <= 0 or NaN culls."""
    from . import tessellator as ts
    mode = {1: ts.INTEGER, 2: ts.POW2, 3: ts.FRACTIONAL_ODD, 4: ts.FRACTIONAL_EVEN}.get(partitioning, ts.INTEGER)
    kind_out = ts.OUTPUT_POINT if output == 1 else ts.OUTPUT_LINE if domain == 1 else ts.OUTPUT_TRIANGLE_CW
    t = ts.Tessellator(mode, kind_out)
    f = [float(v) for v in factors] + [1.0] * 4
    i = [float(v) for v in inside] + [1.0] * 2
    if domain == 2:
        t.tessellate_tri(f[0], f[1], f[2], i[0])
    elif domain == 3:
        t.tessellate_quad(f[0], f[1], f[2], f[3], i[0], i[1])
    else:
        t.tessellate_isoline(f[0], f[1])
    uv = numpy.array(t.points[:t.num_points], dtype=numpy.float64).reshape(-1, 2)
    third = 1.0 - uv[:, 0] - uv[:, 1] if domain == 2 else numpy.zeros(len(uv))
    points = numpy.concatenate([uv, third[:, None]], axis=1) if len(uv) else numpy.zeros((0, 3))
    indices = numpy.array(t.indices[:t.num_indices], dtype=numpy.int64)
    if kind_out == ts.OUTPUT_POINT:
        return points, indices.reshape(-1, 1), "points"
    if kind_out == ts.OUTPUT_LINE:
        return points, indices.reshape(-1, 2), "lines"
    return points, indices.reshape(-1, 3), "triangles"
