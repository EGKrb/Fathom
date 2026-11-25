# Fathom addition to dumpulator: an interpreter of DXIL, the shaders of shader model 6 (dxc's LLVM IR, read
# by dxil_bitcode.py), for the Direct3D 12 of d3d12_emu.py: vertex, pixel and compute shaders, and the
# functions of a library (ray tracing, dxr.py). Boost Software License 1.0, like the rest of dumpulator
# (LICENSE.dumpulator).
#
# All the lanes (vertices, pixels, threads) run at once: a value is a numpy array per lane (int64 for the
# integers, kept unsigned to their width; float32 for half and float, float64 for double; a tuple for a
# structure). The control flow is the basic blocks', run in reverse post-order with a mask of lanes per
# block (a block runs for the lanes that reached it; a phi takes the value of the block each lane came
# from), which runs the loops and the divergent branches of a reducible graph. Local arrays (alloca) and
# groupshared variables are memories of scalar cells (one row per lane, or per thread group). The dx.op
# functions are the operations of DXIL: inputs / outputs by signature element, constant buffers, textures
# (sample, load, store, gather), typed, raw and structured buffers, atomics, thread ids, derivatives (from
# the quads of the pixel stage), wave operations (a wave: 32 lanes in order), math.

import struct
from typing import Dict, List, Optional

import numpy

from . import dxil_bitcode as bc
from .dxbc import Bindings, Result, Unsupported, chunks, signature
from .dxil_bitcode import Type

WAVE = 32
# the operations that write memory or reach the descriptor heaps
SIDE_EFFECTS = {67, 69, 70, 78, 79, 140, 174, 175, 176, 177, 218, 225, 304}
OPS: Dict[int, str] = {}


def _width(t: Type) -> int:
    return t.bits if t.kind == "int" else 32


def _is_float(t: Type) -> bool:
    return t.kind in ("float", "half", "double")


def _dtype(t: Type):
    if t.kind == "double":
        return numpy.float64
    if t.kind in ("float", "half"):
        return numpy.float32
    return numpy.int64


def _mask(values, bits):
    if bits >= 64:
        return values.astype(numpy.int64)
    return values.astype(numpy.int64) & ((1 << bits) - 1)


def _signed(values, bits):
    values = values.astype(numpy.int64)
    if bits >= 64:
        return values
    sign = 1 << (bits - 1)
    return ((values & ((1 << bits) - 1)) ^ sign) - sign


def _slots(t: Type) -> int:
    if t.kind in ("array", "vector"):
        return t.count * _slots(t.elements[0])
    if t.kind == "struct":
        return sum(_slots(e) for e in t.elements)
    return 1


def _scalars(t: Type) -> List[Type]:
    if t.kind in ("array", "vector"):
        return _scalars(t.elements[0]) * t.count
    if t.kind == "struct":
        return [s for e in t.elements for s in _scalars(e)]
    return [t]


class Memory:
    """Scalar cells, a row per lane (alloca) or per thread group (groupshared)."""

    def __init__(self, t: Type, rows: int):
        self.types = _scalars(t)
        self.cells = [numpy.zeros(rows, dtype=_dtype(s)) for s in self.types]


class Pointer:
    __slots__ = ("memory", "offset", "rows")

    def __init__(self, memory, offset, rows):
        self.memory, self.offset, self.rows = memory, offset, rows


class Handle:
    """A resource of a shader: class (0 SRV, 1 UAV, 2 CBV, 3 sampler), register and space."""
    __slots__ = ("klass", "register", "kind", "stride", "space")

    def __init__(self, klass, register, kind=0, stride=0, space=0):
        self.klass, self.register, self.kind, self.stride, self.space = klass, register, kind, stride, space


def _lookup(table, handle):
    """A binding of a handle: by (space, register) first when its space is not 0, then by register."""
    if handle.space:
        found = table.get((handle.space, handle.register))
        if found is not None:
            return found
    return table.get(handle.register)


class ResourceGlobal:
    """The global variable of a resource in a library (loaded, then given to createHandleForLib)."""
    __slots__ = ("klass", "ident")

    def __init__(self, klass, ident):
        self.klass, self.ident = klass, ident


class Shader:
    """A DXIL shader (a DXBC container with a DXIL part): its signatures and module, for the pipeline of
    d3d_pipeline.py (the fields of dxbc.Shader that it reads)."""
    dxil = True

    def __init__(self, bytecode: bytes, entry: Optional[str] = None):
        parts = chunks(bytecode)
        self.parts = parts                  # the container's parts (RDAT: the subobjects of a library)
        self.error = None
        self.inputs = signature(parts.get(b"ISG1") or parts.get(b"ISGN"), b"ISG1" if b"ISG1" in parts else b"ISGN")
        self.outputs = signature(parts.get(b"OSG1") or parts.get(b"OSGN"), b"OSG1" if b"OSG1" in parts else b"OSGN")
        self.patch = signature(parts.get(b"PSG1"), b"PSG1") if b"PSG1" in parts else []
        self.root_signature = parts.get(b"RTS0")
        self.kind, self.model, code = bc.program(parts[b"DXIL"])
        self.sm51 = True
        self.module = bc.Module(code)
        m = self.module
        self.thread_group = (1, 1, 1)
        self.resources: Dict[int, dict] = {}
        self.uavs: Dict[int, dict] = {}
        self.tgsm = {}
        self.phases = []
        self.gs_input, self.gs_output, self.gs_instances = 3, 5, 1
        self.input_cp = self.output_cp = 0
        self.domain, self.partitioning, self.output_primitive = 2, 1, 3
        # the resources of the metadata: class -> {id: (space, lower, kind, stride, global)}
        self.ranges: Dict[int, Dict[int, tuple]] = {0: {}, 1: {}, 2: {}, 3: {}}
        self.resource_globals: Dict[int, tuple] = {}     # the global of a resource (libraries) -> (class, id)
        groups = (m.named.get("dx.resources") or [None])[0] or []
        for klass, group in enumerate(list(groups) + [None] * (4 - len(groups))):
            for record in group or []:
                if not isinstance(record, list) or len(record) < 6:
                    continue
                ident, symbol = self._int(record[0]), record[1]
                space, lower = self._int(record[3]), self._int(record[4])
                kind = self._int(record[6]) if klass in (0, 1) and len(record) > 6 else 0
                stride = 0
                tags = record[8] if klass == 0 and len(record) > 8 else record[10] if klass == 1 and len(record) > 10 else None
                if isinstance(tags, list):
                    for k in range(0, len(tags) - 1, 2):
                        if self._int(tags[k]) == 1:
                            stride = self._int(tags[k + 1])
                self.ranges[klass][ident] = (space, lower, kind, stride, symbol)
                while isinstance(symbol, bc.Value) and symbol.kind == "const" and isinstance(symbol.value, tuple) \
                        and symbol.value[0] in ("cast", "gep"):
                    inner = symbol.value[2] if symbol.value[0] == "cast" else symbol.value[1][0]
                    symbol = m.values[inner] if isinstance(inner, int) and inner < len(m.values) else None
                if isinstance(symbol, bc.Value) and symbol.kind == "global":
                    self.resource_globals[symbol.id] = (klass, ident)
                if klass == 0:
                    self.resources[lower] = dict(dimension="2d", stride=stride)
                elif klass == 1:
                    self.uavs[lower] = dict(dimension="2d", stride=stride)
        # the entry points: name -> (function, signatures, properties)
        self.entries: Dict[str, tuple] = {}
        self.patch_function = None          # hull shader: the patch constant function
        self.mesh = None                    # mesh shader: (max vertices, max primitives, topology, payload size)
        self.payload_size = 0               # amplification shader
        self.max_vertices = 0               # geometry shader
        for record in m.named.get("dx.entryPoints") or []:
            if not isinstance(record, list) or not record:
                continue
            function = record[0].value if isinstance(record[0], bc.Value) and record[0].kind == "function" else None
            name = record[1] if len(record) > 1 and isinstance(record[1], str) else ""
            properties = record[4] if len(record) > 4 and isinstance(record[4], list) else []
            self.entries[name] = (function, record[2] if len(record) > 2 else None, properties)
            for k in range(0, len(properties) - 1, 2):
                self._property(self._int(properties[k]), properties[k + 1])
        definitions = [f for f in m.functions if not f.declaration]
        if entry is not None:
            chosen = next((e[0] for n, e in self.entries.items() if n == entry), None)
        else:
            chosen = next((e[0] for e in self.entries.values() if e[0] is not None), None)
        self.function = chosen or (definitions[0] if definitions else None)
        self.interpolation: Dict[int, int] = {}       # input register -> interpolation mode (as dxbc's)
        self.input_elements, self.output_elements = self._elements(0), self._elements(1)
        self.patch_elements = self._elements(2)
        # writes memory (stores, atomics, counters) or reaches the descriptor heaps: not shaded by several threads
        self.side_effects = bool(self.function is not None and self._operations() & SIDE_EFFECTS)
        # groupshared variables (address space 3)
        for value in m.values:
            if value.kind == "global" and value.type.space == 3:
                self.tgsm[value.id] = value.type.pointee

    def _operations(self):
        """The dx.op opcodes the entry function calls."""
        found = set()
        for block in self.function.blocks:
            for ins in block:
                if ins.op == "call" and ins.operands:
                    callee = self.function.all_values[ins.extra["callee"]]
                    if (callee.name or "").startswith("dx.op."):
                        value = self.function.all_values[ins.operands[0]]
                        if value.kind == "const" and isinstance(value.value, int):
                            found.add(value.value)
        return found

    def _property(self, tag, value):
        """A shader property of an entry point (DxilMetadataHelper's tags)."""
        if not isinstance(value, list):
            return
        if tag == 4:                        # NumThreads
            self.thread_group = tuple(self._int(v) for v in value[:3])
        elif tag == 1:                      # GS: input primitive, max vertices, streams, topology, instances
            self.gs_input, self.max_vertices = self._int(value[0]), self._int(value[1])
            self.gs_output = self._int(value[3]) or 5
            self.gs_instances = max(1, self._int(value[4])) if len(value) > 4 else 1
        elif tag == 2:                      # DS: domain, input control points
            self.domain, self.input_cp = self._int(value[0]), self._int(value[1])
        elif tag == 3:                      # HS: patch constant function, input / output points, domain, ...
            patch = value[0]
            self.patch_function = patch.value if isinstance(patch, bc.Value) and patch.kind == "function" else None
            self.input_cp, self.output_cp = self._int(value[1]), self._int(value[2])
            self.domain, self.partitioning, self.output_primitive = (self._int(v) for v in value[3:6])
        elif tag == 9:                      # MS: threads, max vertices, max primitives, topology, payload
            if isinstance(value[0], list):
                self.thread_group = tuple(self._int(v) for v in value[0][:3])
            self.mesh = tuple(self._int(v) for v in value[1:5])
        elif tag == 10:                     # AS: threads, payload
            if isinstance(value[0], list):
                self.thread_group = tuple(self._int(v) for v in value[0][:3])
            self.payload_size = self._int(value[1]) if len(value) > 1 else 0

    def _int(self, item) -> int:
        value = self.module.constant(item)
        if isinstance(value, tuple):
            return 0
        if isinstance(value, (int, float)):
            return int(value)
        return 0

    def _elements(self, which):
        """The signature elements of the entry (id -> (register, first component, name, index))."""
        out = {}
        if self.function is None:
            return out
        signatures = next((e[1] for e in self.entries.values() if e[0] is self.function), None)
        if not isinstance(signatures, list) or len(signatures) <= which or not isinstance(signatures[which], list):
            return out
        table = (self.inputs, self.outputs, self.patch)[which]
        for element in signatures[which]:
            if not isinstance(element, list) or len(element) < 10:
                continue
            ident = self._int(element[0])
            name = element[1] if isinstance(element[1], str) else ""
            indices = element[4] if isinstance(element[4], list) else []
            first_index = self._int(indices[0]) if indices else 0
            row_value = self.module.constant(element[8])
            row = int(row_value) if isinstance(row_value, int) else -1
            column = self._int(element[9])
            if row < 0 or row >= 1 << 30:
                found = next((e for e in table if e["name"].upper() == name.upper() and e["index"] == first_index), None)
                row = found["register"] if found else 0
                if found and found["mask"]:
                    column = (found["mask"] & -found["mask"]).bit_length() - 1
            out[ident] = (row, column, name, first_index)
            if which == 0:
                rows = max(1, self._int(element[6]))
                for r in range(rows):
                    self.interpolation[row + r] = self._int(element[5])
        return out

    def check(self):
        if self.function is None:
            raise Unsupported("a DXIL shader without an entry point")


# ------------------------------------------------------------------ the interpreter
class Machine:
    def __init__(self, shader: Shader, function, n: int, bindings: Bindings, inputs=None, system=None, groups=None,
                 group_count=1, helper=None, inputs2d=None, vocp=None, vpc=None):
        self.s, self.f, self.n = shader, function, n
        self.inputs2d = inputs2d or {}      # register -> (n, vertices, 4): GS inputs, HS / DS control points
        self.vocp = vocp or {}              # register -> (n, points, 4): the output control points (patch function)
        self.vpc = vpc or {}                # register -> (n, 4): the patch constants (domain shader)
        self.patch_outputs = None           # hull shader: register -> (patches, 4) written by the patch function
        self.m = shader.module
        self.b = bindings or Bindings()
        self.inputs = inputs or {}
        self.system = system or {}
        self.values: Dict[int, object] = {}
        self.result = Result()
        self.discarded = numpy.zeros(n, dtype=bool)
        self.helper = numpy.zeros(n, dtype=bool) if helper is None else numpy.asarray(helper, dtype=bool)
        self.groups = numpy.zeros(n, dtype=numpy.int64) if groups is None else groups
        self.shared: Dict[int, Memory] = {gid: Memory(t, group_count) for gid, t in shader.tgsm.items()}
        self.globals: Dict[int, Memory] = {}
        self.callbacks = {}          # opcode -> function (ray tracing)

    # values ------------------------------------------------------------------
    def constant(self, value: bc.Value):
        v = value.value
        t = value.type
        n = self.n
        if isinstance(v, tuple):
            if v[0] in ("null", "undef"):
                return self._zero(t)
            if v[0] == "aggregate":
                return tuple(self.get(i) for i in v[1])
            if v[0] == "data":
                return tuple(numpy.full(n, item, dtype=_dtype(t.elements[0])) if not _is_float(t.elements[0]) else
                             numpy.full(n, struct.unpack("<f", struct.pack("<I", item & 0xFFFFFFFF))[0], dtype=numpy.float32)
                             for item in v[1])
            if v[0] == "gep":
                return self.get(v[1][0])
            if v[0] == "cast":
                return self.get(v[2])
            return self._zero(t)
        if _is_float(t):
            return numpy.full(n, v, dtype=_dtype(t))
        return numpy.full(n, int(v) & ((1 << t.bits) - 1) if t.bits < 64 else int(v), dtype=numpy.int64)

    def _zero(self, t: Type):
        if t.kind == "struct":
            return tuple(self._zero(e) for e in t.elements)
        if t.kind in ("array", "vector"):
            return tuple(self._zero(t.elements[0]) for _ in range(t.count))
        return numpy.zeros(self.n, dtype=_dtype(t))

    def get(self, index: int):
        if index in self.values:
            return self.values[index]
        value = self.f.all_values[index] if index < len(self.f.all_values) else None
        if value is None:
            raise Unsupported(f"DXIL value {index}")
        if value.kind == "const":
            data = self.constant(value)
            self.values[index] = data
            return data
        if value.kind == "global":
            return self._global(value)
        if value.kind == "function":
            return value.value
        return self._zero(value.type)

    def _global(self, value: bc.Value):
        resource = self.s.resource_globals.get(value.id)
        if resource is not None:
            return ResourceGlobal(*resource)
        pointee = value.type.pointee
        if value.type.space == 3:
            memory = self.shared.get(value.id)
            if memory is None:
                memory = self.shared[value.id] = Memory(pointee, int(self.groups.max()) + 1 if self.n else 1)
            return Pointer(memory, numpy.zeros(self.n, dtype=numpy.int64), self.groups)
        if value.id not in self.globals:
            memory = Memory(pointee, 1)
            init = value.value.get("init") if isinstance(value.value, dict) else None
            if init is not None and init < len(self.m.values):
                flat = self._flatten(self.m.values[init])
                for k, item in enumerate(flat[:len(memory.cells)]):
                    memory.cells[k][0] = item
            self.globals[value.id] = memory
        return Pointer(self.globals[value.id], numpy.zeros(self.n, dtype=numpy.int64), numpy.zeros(self.n, dtype=numpy.int64))

    def _flatten(self, value: bc.Value):
        v = value.value
        if isinstance(v, tuple) and v[0] == "aggregate":
            return [x for i in v[1] for x in self._flatten(self.m.values[i])]
        if isinstance(v, tuple) and v[0] == "data":
            t = value.type.elements[0]
            return [struct.unpack("<f", struct.pack("<I", x & 0xFFFFFFFF))[0] if t.kind == "float" else x for x in v[1]]
        if isinstance(v, tuple):
            return [0] * _slots(value.type)
        return [v]

    # running -----------------------------------------------------------------
    def run(self, mask=None, args=None) -> Result:
        f = self.f
        if args:
            for value, data in zip(f.args, args):
                self.values[value.id] = data
        blocks = f.blocks
        order = self._order()
        rank = {b: k for k, b in enumerate(order)}
        pending = {b: numpy.zeros(self.n, dtype=bool) for b in range(len(blocks))}
        came = numpy.full(self.n, -1, dtype=numpy.int64)
        pending[0] = numpy.ones(self.n, dtype=bool) if mask is None else mask.copy()
        steps = 0
        with numpy.errstate(all="ignore"):
            while True:
                ready = [b for b in range(len(blocks)) if pending[b].any()]
                if not ready:
                    break
                steps += 1
                if steps > 1 << 20:
                    raise Unsupported("a DXIL shader that does not end")
                block = min(ready, key=lambda b: rank.get(b, b))
                lanes = pending[block]
                pending[block] = numpy.zeros(self.n, dtype=bool)
                self._block(block, lanes, came, pending)
        return self.result

    def _order(self):
        blocks = self.f.blocks
        successors = []
        for instructions in blocks:
            last = instructions[-1] if instructions else None
            if last is None:
                successors.append([])
            elif last.op == "br":
                successors.append(list(last.extra["targets"]))
            elif last.op == "switch":
                successors.append([last.extra["default"]] + [b for _, b in last.extra["cases"]])
            else:
                successors.append([])
        seen, post = set(), []

        def visit(b):
            stack = [(b, iter(successors[b]))]
            seen.add(b)
            while stack:
                node, it = stack[-1]
                for nxt in it:
                    if nxt not in seen and nxt < len(blocks):
                        seen.add(nxt)
                        stack.append((nxt, iter(successors[nxt])))
                        break
                else:
                    post.append(node)
                    stack.pop()
        visit(0)
        return list(reversed(post)) + [b for b in range(len(blocks)) if b not in seen]

    def _set(self, ins, value, lanes):
        if ins.id is None:
            return
        old = self.values.get(ins.id)
        if old is None or lanes.all():
            self.values[ins.id] = value
            return
        self.values[ins.id] = _merge(lanes, value, old)

    def _block(self, block, lanes, came, pending):
        instructions = self.f.blocks[block]
        # the phis first, all from the values before the block
        phis = []
        for ins in instructions:
            if ins.op != "phi":
                break
            out = None
            for value, source in zip(ins.operands, ins.extra["blocks"]):
                from_here = lanes & (came == source)
                if not from_here.any():
                    continue
                data = self.get(value)
                out = data if out is None else _merge(from_here, data, out)
            phis.append((ins, out if out is not None else self._zero(ins.type)))
        for ins, value in phis:
            self._set(ins, value, lanes)
        for ins in instructions[len(phis):]:
            active = lanes & ~self.discarded
            op = ins.op
            if op == "br":
                targets = ins.extra["targets"]
                if len(targets) == 1:
                    pending[targets[0]] |= lanes
                    came[lanes] = block
                else:
                    condition = self.get(ins.operands[0]).astype(bool)
                    yes, no = lanes & condition, lanes & ~condition
                    pending[targets[0]] |= yes
                    pending[targets[1]] |= no
                    came[lanes] = block
                return
            if op == "switch":
                value = self.get(ins.operands[0])
                rest = lanes.copy()
                for case, target in ins.extra["cases"]:
                    hit = rest & (value == int(self.m.constant(self.f.all_values[case])) if isinstance(case, int) and
                                  case < len(self.f.all_values) else rest & False)
                    pending[target] |= hit
                    rest &= ~hit
                pending[ins.extra["default"]] |= rest
                came[lanes] = block
                return
            if op == "ret":
                if ins.operands:
                    value = self.get(ins.operands[0])
                    self.returned = value if getattr(self, "returned", None) is None else _merge(lanes, value, self.returned)
                return
            if op == "unreachable":
                return
            self._instruction(ins, lanes, active)

    # instructions --------------------------------------------------------------
    def _instruction(self, ins, lanes, active):
        op = ins.op
        if op == "binop":
            self._set(ins, self._binop(ins), lanes)
        elif op == "cast":
            self._set(ins, self._cast(ins), lanes)
        elif op == "cmp":
            self._set(ins, self._cmp(ins), lanes)
        elif op == "select":
            c, a, b = (self.get(i) for i in ins.operands)
            if isinstance(c, tuple):        # a vector condition: element by element
                self._set(ins, tuple(_merge(x.astype(bool), y, z) for x, y, z in zip(c, a, b)), lanes)
            else:
                self._set(ins, _merge(c.astype(bool), a, b), lanes)
        elif op == "extractvalue":
            value = self.get(ins.operands[0])
            for index in ins.extra["indices"]:
                value = value[index]
            self._set(ins, value, lanes)
        elif op == "insertvalue":
            value = list(self.get(ins.operands[0]))
            value[ins.extra["indices"][0]] = self.get(ins.operands[1])
            self._set(ins, tuple(value), lanes)
        elif op == "extractelement":
            vector, index = self.get(ins.operands[0]), self.get(ins.operands[1])
            out = vector[0].copy()
            for k, item in enumerate(vector):
                out = numpy.where(index == k, item, out)
            self._set(ins, out, lanes)
        elif op == "insertelement":
            vector, value, index = self.get(ins.operands[0]), self.get(ins.operands[1]), self.get(ins.operands[2])
            self._set(ins, tuple(numpy.where(index == k, value, item) for k, item in enumerate(vector)), lanes)
        elif op == "alloca":
            if ins.id not in self.values:
                memory = Memory(ins.type.pointee, self.n)
                self.values[ins.id] = Pointer(memory, numpy.zeros(self.n, dtype=numpy.int64), numpy.arange(self.n))
        elif op == "gep":
            self._set(ins, self._gep(ins), lanes)
        elif op == "load":
            self._set(ins, self._load(self.get(ins.operands[0]), ins.type), lanes)
        elif op == "store":
            self._store(self.get(ins.operands[0]), self.get(ins.operands[1]), active & ~self.helper)
        elif op == "atomicrmw":
            self._set(ins, self._atomic_memory(self.get(ins.operands[0]), ins.extra["op"], self.get(ins.operands[1]), None,
                                               active & ~self.helper), lanes)
        elif op == "cmpxchg":
            old = self._atomic_memory(self.get(ins.operands[0]), "cmpxchg", self.get(ins.operands[2]), self.get(ins.operands[1]),
                                      active & ~self.helper)
            self._set(ins, (old, (old == self.get(ins.operands[1])).astype(numpy.int64)), lanes)
        elif op == "call":
            callee = self.f.all_values[ins.extra["callee"]]
            name = callee.name or (callee.value.name if callee.kind == "function" else "")
            if name.startswith("dx.op."):
                opcode = int(self.m.constant(self.f.all_values[ins.operands[0]]))
                args = [self.get(i) for i in ins.operands[1:]]
                result = self._dx(opcode, args, ins, lanes, active)
                if result is not None:
                    self._set(ins, result, lanes)
            elif name.startswith("llvm."):
                self._set(ins, self._llvm(name, [self.get(i) for i in ins.operands]), lanes)
            else:
                raise Unsupported(f"DXIL call of {name}")
        else:
            raise Unsupported(f"DXIL instruction {op}")

    def _binop(self, ins):
        a, b = self.get(ins.operands[0]), self.get(ins.operands[1])
        if isinstance(a, tuple) or isinstance(b, tuple):     # vectors (shader model 6.9): element by element
            count = len(a) if isinstance(a, tuple) else len(b)
            a, b = (v if isinstance(v, tuple) else (v,) * count for v in (a, b))
            return tuple(self._binop_values(ins.extra["op"], x, y, ins.type.elements[0]) for x, y in zip(a, b))
        return self._binop_values(ins.extra["op"], a, b, ins.type)

    def _binop_values(self, op, a, b, t):
        if op in ("fadd", "fsub", "fmul", "fdiv", "frem"):
            dtype = _dtype(t)
            a, b = a.astype(dtype), b.astype(dtype)
            return {"fadd": a + b, "fsub": a - b, "fmul": a * b, "fdiv": a / b,
                    "frem": numpy.fmod(a, b)}[op].astype(dtype)
        bits = _width(t)
        if op == "add":
            out = a + b
        elif op == "sub":
            out = a - b
        elif op == "mul":
            out = a * b
        elif op == "udiv":
            out = numpy.where(b != 0, _mask(a, bits) // numpy.where(b == 0, 1, _mask(b, bits)), (1 << min(bits, 63)) - 1)
        elif op == "sdiv":
            sa, sb = _signed(a, bits), _signed(b, bits)
            out = numpy.where(sb != 0, numpy.trunc(sa / numpy.where(sb == 0, 1, sb)).astype(numpy.int64), -1)
        elif op == "urem":
            out = numpy.where(b != 0, _mask(a, bits) % numpy.where(b == 0, 1, _mask(b, bits)), _mask(a, bits))
        elif op == "srem":
            sa, sb = _signed(a, bits), _signed(b, bits)
            out = numpy.where(sb != 0, numpy.fmod(sa, numpy.where(sb == 0, 1, sb)), sa)
        elif op == "shl":
            out = _mask(a, bits) << (b & (bits - 1))
        elif op == "lshr":
            out = _mask(a, bits) >> (b & (bits - 1))
        elif op == "ashr":
            out = _signed(a, bits) >> (b & (bits - 1))
        elif op == "and":
            out = a & b
        elif op == "or":
            out = a | b
        else:
            out = a ^ b
        return _mask(out, bits)

    def _cast(self, ins):
        a = self.get(ins.operands[0])
        op = ins.extra["op"]
        source = self.f.all_values[ins.operands[0]].type
        t = ins.type
        if isinstance(a, tuple) and source.kind == "vector" and t.kind == "vector" and len(a) == t.count:
            return tuple(self._cast_values(op, x, source.elements[0], t.elements[0]) for x in a)
        return self._cast_values(op, a, source, t)

    def _cast_values(self, op, a, source, t):
        if op in ("bitcast", "addrspacecast", "ptrtoint", "inttoptr"):
            if isinstance(a, (Pointer, Handle, tuple)) or op != "bitcast":
                return a
            if _is_float(t) and not _is_float(source):
                width = t.bits
                return (_mask(a, width).astype(numpy.uint32 if width == 32 else numpy.uint64 if width == 64 else numpy.uint16)
                        .view(numpy.float32 if width == 32 else numpy.float64 if width == 64 else numpy.float16)).astype(_dtype(t))
            if not _is_float(t) and _is_float(source):
                width = source.bits
                raw = a.astype(numpy.float32 if width == 32 else numpy.float64 if width == 64 else numpy.float16)
                return raw.view(numpy.uint32 if width == 32 else numpy.uint64 if width == 64 else numpy.uint16).astype(numpy.int64)
            return a
        if op == "trunc":
            return _mask(a, t.bits)
        if op == "zext":
            return _mask(a, source.bits)
        if op == "sext":
            return _mask(_signed(a, source.bits), t.bits)
        if op in ("fptoui", "fptosi"):
            value = numpy.nan_to_num(a.astype(numpy.float64), nan=0.0)
            if op == "fptoui":
                value = numpy.clip(value, 0, (1 << t.bits) - 1)
            else:
                value = numpy.clip(value, -(1 << (t.bits - 1)), (1 << (t.bits - 1)) - 1)
            return _mask(numpy.trunc(value).astype(numpy.int64), t.bits)
        if op == "uitofp":
            return _mask(a, source.bits).astype(numpy.float64).astype(_dtype(t))
        if op == "sitofp":
            return _signed(a, source.bits).astype(numpy.float64).astype(_dtype(t))
        if op in ("fptrunc", "fpext"):
            if t.kind == "half":
                return a.astype(numpy.float16).astype(numpy.float32)
            return a.astype(_dtype(t))
        return a

    def _cmp(self, ins):
        a, b = self.get(ins.operands[0]), self.get(ins.operands[1])
        t = self.f.all_values[ins.operands[0]].type
        if isinstance(a, tuple) or isinstance(b, tuple):
            count = len(a) if isinstance(a, tuple) else len(b)
            a, b = (v if isinstance(v, tuple) else (v,) * count for v in (a, b))
            element = t.elements[0] if t.kind == "vector" else t
            return tuple(self._cmp_values(ins.extra["predicate"], x, y, element) for x, y in zip(a, b))
        return self._cmp_values(ins.extra["predicate"], a, b, t)

    def _cmp_values(self, p, a, b, t):
        if p < 32:      # fcmp: 1 oeq, 2 ogt, 3 oge, 4 olt, 5 ole, 6 one, 7 ord, 8 uno, 9 ueq ... 14 une, 15 true
            nan = numpy.isnan(a) | numpy.isnan(b)
            base = {1: a == b, 2: a > b, 3: a >= b, 4: a < b, 5: a <= b, 6: a != b, 7: ~nan, 8: nan,
                    9: a == b, 10: a > b, 11: a >= b, 12: a < b, 13: a <= b, 14: a != b}
            if p == 0:
                out = numpy.zeros(self.n, dtype=bool)
            elif p == 15:
                out = numpy.ones(self.n, dtype=bool)
            elif p <= 6:
                out = base[p] & ~nan
            elif p in (7, 8):
                out = base[p]
            else:
                out = base[p] | nan
            return out.astype(numpy.int64)
        bits = _width(t)
        if p in (32, 33):
            out = (_mask(a, bits) == _mask(b, bits)) if p == 32 else (_mask(a, bits) != _mask(b, bits))
        elif p in (34, 35, 36, 37):     # ugt uge ult ule
            ua, ub = _mask(a, bits), _mask(b, bits)
            if bits >= 64:
                ua, ub = ua.astype(numpy.uint64), ub.astype(numpy.uint64)
            out = {34: ua > ub, 35: ua >= ub, 36: ua < ub, 37: ua <= ub}[p]
        else:                           # sgt sge slt sle
            sa, sb = _signed(a, bits), _signed(b, bits)
            out = {38: sa > sb, 39: sa >= sb, 40: sa < sb, 41: sa <= sb}[p]
        return out.astype(numpy.int64)

    # memory ----------------------------------------------------------------------
    def _gep(self, ins):
        pointer = self.get(ins.operands[0])
        if not isinstance(pointer, Pointer):
            return pointer
        t = ins.extra["source"]
        indices = [self.get(i) for i in ins.operands[1:]]
        offset = pointer.offset + _signed(indices[0], 32) * _slots(t) if indices else pointer.offset.copy()
        for k, index in enumerate(indices[1:]):
            if t.kind == "struct":
                field = self.m.constant(self.f.all_values[ins.operands[2 + k]])
                field = int(field) if isinstance(field, (int, float)) else 0       # a null constant: 0
                offset = offset + sum(_slots(e) for e in t.elements[:field])
                t = t.elements[field]
            else:
                t = t.elements[0]
                offset = offset + _signed(index, 32) * _slots(t)
        return Pointer(pointer.memory, offset, pointer.rows)

    def _load(self, pointer, t: Type):
        if isinstance(pointer, ResourceGlobal):
            lower = self.s.ranges.get(pointer.klass, {}).get(pointer.ident, (0, 0))[1]
            return self._handle(pointer.klass, pointer.ident, lower)
        if not isinstance(pointer, Pointer):
            return self._zero(t)
        if t.kind in ("struct", "array", "vector"):
            items, base = [], pointer.offset
            elements = t.elements if t.kind == "struct" else [t.elements[0]] * t.count
            for e in elements:
                items.append(self._load(Pointer(pointer.memory, base, pointer.rows), e))
                base = base + _slots(e)
            return tuple(items)
        cells = pointer.memory.cells
        out = numpy.zeros(self.n, dtype=_dtype(t))
        offset = numpy.clip(pointer.offset, 0, len(cells) - 1)
        rows = numpy.broadcast_to(pointer.rows, (self.n,))
        for k in numpy.unique(offset):
            sel = offset == k
            out[sel] = cells[int(k)][rows[sel]]
        return out

    def _store(self, pointer, value, lanes):
        if not isinstance(pointer, Pointer) or not lanes.any():
            return
        if isinstance(value, tuple):
            base = pointer.offset
            for item in value:
                self._store(Pointer(pointer.memory, base, pointer.rows), item, lanes)
                base = base + (_slots_of_value(item))
            return
        cells = pointer.memory.cells
        offset = pointer.offset
        rows = numpy.broadcast_to(pointer.rows, (self.n,))
        for k in numpy.unique(offset[lanes]):
            if not 0 <= k < len(cells):
                continue
            sel = numpy.nonzero(lanes & (offset == k))[0]
            cells[int(k)][rows[sel]] = value[sel]

    def _atomic_memory(self, pointer, op, value, compare, lanes):
        """atomicrmw / cmpxchg on a groupshared (or local) cell, lane after lane: the old values."""
        out = numpy.zeros(self.n, dtype=numpy.int64)
        if not isinstance(pointer, Pointer):
            return out
        cells = pointer.memory.cells
        rows = numpy.broadcast_to(pointer.rows, (self.n,))
        for lane in numpy.nonzero(lanes)[0]:
            k = int(pointer.offset[lane])
            if not 0 <= k < len(cells):
                continue
            cell, row = cells[k], int(rows[lane])
            old = int(cell[row])
            out[lane] = old
            cell[row] = _rmw(op, old, int(value[lane]), int(compare[lane]) if compare is not None else 0)
        return out

    # llvm intrinsics -----------------------------------------------------------------
    def _llvm(self, name, args):
        if name.startswith("llvm.lifetime") or name.startswith("llvm.dbg") or name.startswith("llvm.donothing"):
            return None
        raise Unsupported(f"DXIL call of {name}")

    # resources -----------------------------------------------------------------------
    def _handle(self, klass, ident, index):
        space, lower, kind, stride, symbol = self.s.ranges.get(klass, {}).get(ident, (0, ident, 0, 0, None))
        register = int(index[0]) if isinstance(index, numpy.ndarray) else int(index)
        if register < lower:
            register += lower
        return Handle(klass, register, kind, stride, space)

    def _heap(self, handle):
        """What a handle of ResourceDescriptorHeap / SamplerDescriptorHeap designates (None when unbound)."""
        if isinstance(handle.register, tuple):
            resolve = getattr(self.b, "heap_resource", None)
            return resolve(handle.register[2], handle.register[1]) if resolve is not None else None
        return None

    def _view(self, handle):
        if not isinstance(handle, Handle):
            return None
        if isinstance(handle.register, tuple):
            found = self._heap(handle)
            return found if hasattr(found, "kind") and hasattr(found, "bytes") else None
        table = {0: self.b.srvs, 1: self.b.uavs}.get(handle.klass)
        return _lookup(table, handle) if table is not None else None

    def _cb(self, handle):
        if not isinstance(handle, Handle):
            return None
        if isinstance(handle.register, tuple):
            found = self._heap(handle)
            return found if isinstance(found, numpy.ndarray) else None
        return _lookup(self.b.cbs, handle)

    def _sampler(self, handle):
        if not isinstance(handle, Handle):
            return None
        if isinstance(handle.register, tuple):
            found = self._heap(handle)
            return None if found is None or isinstance(found, numpy.ndarray) or hasattr(found, "bytes") else found
        return _lookup(self.b.samplers, handle)

    def _bytes(self, handle):
        view = self._view(handle)
        if view is None or view.bytes is None:
            return None, view
        return view.bytes, view

    def _dx(self, opcode, args, ins, lanes, active):
        handler = _DX.get(opcode)
        if handler is None:
            callback = self.callbacks.get(opcode)
            if callback is not None:
                return callback(self, args, ins, lanes, active)
            raise Unsupported(f"DXIL operation {OPS.get(opcode, opcode)}")
        return handler(self, args, ins, lanes, active)


def _slots_of_value(value):
    if isinstance(value, tuple):
        return sum(_slots_of_value(v) for v in value)
    return 1


def _merge(mask, new, old):
    if hasattr(new, "merge"):           # an object of its own (a HitObject): it merges itself
        return new.merge(numpy.asarray(mask, dtype=bool), old)
    if isinstance(new, tuple):
        return tuple(_merge(mask, a, b) for a, b in zip(new, old))
    if isinstance(new, (Pointer, Handle)) or not isinstance(new, numpy.ndarray):
        return new
    if not isinstance(old, numpy.ndarray) or old.shape != new.shape:
        return new
    return numpy.where(mask, new, old.astype(new.dtype))


def _rmw(op, old, value, compare=0):
    if op in ("add",):
        return (old + value) & 0xFFFFFFFF
    if op == "sub":
        return (old - value) & 0xFFFFFFFF
    if op == "and":
        return old & value
    if op == "or":
        return old | value
    if op == "xor":
        return old ^ value
    if op in ("xchg", "exchange"):
        return value
    if op == "cmpxchg":
        return value if old == compare else old
    if op in ("max", "min"):
        so, sv = (old - (1 << 32) if old & 0x80000000 else old), (value - (1 << 32) if value & 0x80000000 else value)
        return (max(so, sv) if op == "max" else min(so, sv)) & 0xFFFFFFFF
    if op == "umax":
        return max(old, value)
    if op == "umin":
        return min(old, value)
    return old


# ------------------------------------------------------------------ dx.op
def _f32(x):
    return x.astype(numpy.float32)


def _bits_of(value, t: Type):
    """The 32-bit pattern of a value of a type (for the registers of the pipeline)."""
    if _is_float(t):
        return value.astype(numpy.float32).view(numpy.uint32)
    return (value & 0xFFFFFFFF).astype(numpy.uint32)


def _from_bits(bits, t: Type):
    bits = numpy.asarray(bits, dtype=numpy.uint32)
    if t.kind in ("float", "half"):
        return bits.view(numpy.float32).copy()
    if t.kind == "double":
        return bits.view(numpy.float32).astype(numpy.float64)
    return bits.astype(numpy.int64)


def _signature_read(m, elements, table2d, table, args, ins):
    """A signature element (id, row, column[, vertex]) from per-vertex arrays (n, k, 4) or per-lane ones (n, 4)."""
    ident, column = int(numpy.asarray(args[0])[0]), int(numpy.asarray(args[2])[0])
    register, first, _, _ = elements.get(ident, (ident, 0, "", 0))
    rows = register + numpy.broadcast_to(numpy.asarray(args[1], dtype=numpy.int64), (m.n,))
    component = min(first + column, 3)
    vertex = args[3] if len(args) > 3 and isinstance(args[3], numpy.ndarray) else None
    out = numpy.zeros(m.n, dtype=numpy.uint32)
    lane = numpy.arange(m.n)
    for r in numpy.unique(rows):
        sel = rows == r
        data = table2d.get(int(r)) if vertex is not None and table2d else None
        if data is not None:
            k = numpy.clip(numpy.asarray(vertex, dtype=numpy.int64)[sel], 0, data.shape[1] - 1)
            out[sel] = data[lane[sel], k, component]
        else:
            flat = table.get(int(r))
            if flat is not None:
                out[sel] = flat[sel, component] if flat.ndim == 2 else flat[sel, 0, component]
    return _from_bits(out, ins.type)


def _load_input(m, args, ins, lanes, active):
    return _signature_read(m, m.s.input_elements, m.inputs2d, m.inputs, args, ins)


def _store_output(m, args, ins, lanes, active):
    ident, row, column = (int(a[0]) for a in args[:3])
    value = args[3]
    register, first, name, _ = m.s.output_elements.get(ident, (ident, 0, "", 0))
    t = m.f.all_values[ins.operands[4]].type
    if name.upper() == "SV_DEPTH":
        if m.result.depth is None:
            m.result.depth = numpy.zeros(m.n, dtype=numpy.float32)
        m.result.depth = numpy.where(active, value.astype(numpy.float32), m.result.depth)
        return None
    out = m.result.outputs.setdefault(register + row, numpy.zeros((m.n, 4), dtype=numpy.uint32))
    component = min(first + column, 3)
    out[:, component] = numpy.where(active, _bits_of(value, t), out[:, component])
    return None


def _cbuffer_legacy(m, args, ins, lanes, active):
    handle, row = args[0], args[1]
    data = m._cb(handle)
    columns = len(ins.type.elements)
    t = ins.type.elements[0]
    out = []
    rows = numpy.asarray(row).astype(numpy.int64)
    for c in range(columns):
        if data is None or len(data) == 0:
            out.append(numpy.zeros(m.n, dtype=_dtype(t)))
            continue
        index = numpy.clip(rows, 0, len(data) - 1)
        if t.kind == "double":
            low = data[index, (2 * c) % 4].astype(numpy.uint64)
            high = data[index, (2 * c + 1) % 4].astype(numpy.uint64)
            out.append((low | (high << numpy.uint64(32))).view(numpy.float64))
        elif t.kind == "half" or (t.kind == "int" and t.bits == 16):
            word = data[index, c // 2].astype(numpy.uint32) >> numpy.uint32(16 * (c % 2))
            half = (word & 0xFFFF).astype(numpy.uint16)
            out.append(half.view(numpy.float16).astype(numpy.float32) if t.kind == "half" else half.astype(numpy.int64))
        else:
            out.append(_from_bits(data[index, c], t))
    return tuple(out)


def _create_handle(m, args, ins, lanes, active):
    return m._handle(int(args[0][0]), int(args[1][0]), args[2])


def _coords(args, count):
    return numpy.stack([numpy.asarray(a, dtype=numpy.float64) for a in args[:count]], 1)


def _dimensions(view):
    return {"1d": 1, "1darray": 2, "2d": 2, "2darray": 3, "3d": 3, "cube": 3, "cubearray": 4}.get(view.dimension, 2)


def _res_ret(m, values, t: Type):
    base = t.elements[0]
    items = []
    for c in range(4):
        v = values[:, c]
        items.append(v.astype(_dtype(base)) if _is_float(base) else numpy.round(v).astype(numpy.int64) & 0xFFFFFFFF)
    return tuple(items) + (numpy.zeros(m.n, dtype=numpy.int64),)


def _sample(kind):
    def run(m, args, ins, lanes, active):
        from .gpu import DEFAULT_SAMPLER, sample_view
        view, sampler_handle = m._view(args[0]), args[1]
        sampler = m._sampler(sampler_handle)
        if view is None:
            return _res_ret(m, numpy.zeros((m.n, 4)), ins.type)
        count = _dimensions(view)
        coords = _coords(args[2:6], count)
        offsets = [int(numpy.asarray(o)[0]) if isinstance(o, numpy.ndarray) else 0 for o in args[6:8]]
        lod = bias = compare = gradients = None
        rest = args[9:]
        if kind == "level":
            lod = numpy.asarray(rest[0], dtype=numpy.float64)
        elif kind == "bias":
            bias = numpy.asarray(rest[0], dtype=numpy.float64)
        elif kind == "grad":
            gradients = (_coords(rest[0:3], count), _coords(rest[3:6], count))
        elif kind in ("cmp", "cmp_lz"):
            compare = numpy.asarray(rest[0], dtype=numpy.float64)
            if kind == "cmp_lz":
                lod = numpy.zeros(m.n)
        values = sample_view(view, sampler or DEFAULT_SAMPLER, coords, lod, compare, tuple(offsets[:2]) if len(offsets) >= 2 else (0, 0),
                             bias, gradients)
        return _res_ret(m, values, ins.type)
    return run


def _texture_load(m, args, ins, lanes, active):
    from .gpu import load_view
    view = m._view(args[0])
    if view is None:
        return _res_ret(m, numpy.zeros((m.n, 4)), ins.type)
    mip = args[1]
    coords = numpy.stack([numpy.asarray(a, dtype=numpy.int64) for a in args[2:5]], 1)
    values = load_view(view, coords, int(numpy.asarray(mip)[0]) if isinstance(mip, numpy.ndarray) else 0)
    return _res_ret(m, values, ins.type)


def _texture_store(m, args, ins, lanes, active):
    from .gpu import store_view
    view = m._view(args[0])
    if view is None:
        return None
    coords = numpy.stack([numpy.asarray(a, dtype=numpy.int64) for a in args[1:4]], 1)
    values = numpy.stack([numpy.asarray(a, dtype=numpy.float64) for a in args[4:8]], 1)
    store_view(view, coords, values, numpy.nonzero(active & ~m.helper)[0])
    view.dirty = True
    return None


def _address(view, handle, index, offset, n):
    """The byte address of an element of a raw (index: the address) or structured buffer (index * stride + offset)."""
    index = numpy.asarray(index, dtype=numpy.int64)
    stride = view.stride or (handle.stride if isinstance(handle, Handle) else 0)
    structured = view.kind == "structured" or (isinstance(handle, Handle) and handle.kind == 12)
    if structured and stride:
        offset = numpy.asarray(offset, dtype=numpy.int64) if isinstance(offset, numpy.ndarray) else numpy.zeros(n, numpy.int64)
        return index * stride + offset
    return index


def _buffer_load(raw_kind):
    def run(m, args, ins, lanes, active):
        from .gpu import load_view
        data, view = m._bytes(args[0])
        handle = args[0]
        if view is None:
            return _res_ret(m, numpy.zeros((m.n, 4)), ins.type)
        if view.kind == "buffer" or data is None:
            values = load_view(view, numpy.stack([numpy.asarray(args[1], dtype=numpy.int64)] * 3, 1), 0)
            return _res_ret(m, values, ins.type)
        address = _address(view, handle, args[1], args[2], m.n)
        words = data.view(numpy.uint32) if data is not None and len(data) % 4 == 0 else \
            numpy.frombuffer(bytes(data) + b"\0" * (-len(data) % 4), dtype=numpy.uint32)
        base = ins.type.elements[0]
        out = []
        for c in range(4):
            place = address // 4 + c
            ok = (place >= 0) & (place < len(words))
            value = numpy.where(ok, words[numpy.clip(place, 0, max(len(words) - 1, 0))] if len(words) else 0, 0).astype(numpy.uint32)
            out.append(_from_bits(value, base))
        return tuple(out) + (numpy.zeros(m.n, dtype=numpy.int64),)
    return run


def _buffer_store(m, args, ins, lanes, active):
    from .gpu import store_view
    data, view = m._bytes(args[0])
    handle = args[0]
    if view is None:
        return None
    write = numpy.nonzero(active & ~m.helper)[0]
    values = args[3:7]
    mask = int(numpy.asarray(args[7])[0]) if len(args) > 7 and isinstance(args[7], numpy.ndarray) else 0xF
    if view.kind == "buffer" or data is None:
        if view.kind == "buffer":
            store_view(view, numpy.stack([numpy.asarray(args[1], dtype=numpy.int64)] * 3, 1),
                       numpy.stack([numpy.asarray(v, dtype=numpy.float64) for v in values], 1), write)
            view.dirty = True
        return None
    address = _address(view, handle, args[1], args[2], m.n)
    words = data.view(numpy.uint32)
    t = m.f.all_values[ins.operands[4]].type
    for c in range(4):
        if not mask >> c & 1:
            continue
        place = address[write] // 4 + c
        ok = (place >= 0) & (place < len(words))
        words[place[ok]] = _bits_of(numpy.asarray(values[c])[write][ok], t)
    view.dirty = True
    return None


def _atomic_binop(m, args, ins, lanes, active):
    from .gpu import kind_bytes
    data, view = m._bytes(args[0])
    out = numpy.zeros(m.n, dtype=numpy.int64)
    if data is None:
        return out
    op = ["add", "and", "or", "xor", "min", "max", "umin", "umax", "xchg"][int(args[1][0])]
    handle = args[0]
    if view.kind == "buffer":
        address = (numpy.asarray(args[2], dtype=numpy.int64) + view.first) * kind_bytes(view.fmt)
    else:
        address = _address(view, handle, args[2], args[3], m.n)
    words = data.view(numpy.uint32)
    value = numpy.asarray(args[5], dtype=numpy.int64)
    for lane in numpy.nonzero(active & ~m.helper)[0]:
        place = int(address[lane]) // 4
        if 0 <= place < len(words):
            old = int(words[place])
            out[lane] = old
            words[place] = _rmw(op, old, int(value[lane]) & 0xFFFFFFFF)
    view.dirty = True
    return out


def _atomic_cmpxchg(m, args, ins, lanes, active):
    data, view = m._bytes(args[0])
    out = numpy.zeros(m.n, dtype=numpy.int64)
    if data is None:
        return out
    handle = args[0]
    address = _address(view, handle, args[1], args[2], m.n)
    words = data.view(numpy.uint32)
    compare, new = numpy.asarray(args[4], dtype=numpy.int64), numpy.asarray(args[5], dtype=numpy.int64)
    for lane in numpy.nonzero(active & ~m.helper)[0]:
        place = int(address[lane]) // 4
        if 0 <= place < len(words):
            old = int(words[place])
            out[lane] = old
            if old == int(compare[lane]) & 0xFFFFFFFF:
                words[place] = int(new[lane]) & 0xFFFFFFFF
    view.dirty = True
    return out


def _get_dimensions(m, args, ins, lanes, active):
    view = m._view(args[0])
    out = [numpy.zeros(m.n, dtype=numpy.int64) for _ in range(4)]
    if view is not None:
        if view.kind in ("buffer", "raw", "structured"):
            size = view.count or (len(view.bytes) // max(view.stride or 4, 1) if view.bytes is not None else 0)
            if view.kind == "raw":
                size = len(view.bytes) if view.bytes is not None else 0
            out[0][:] = size
        else:
            mip = int(numpy.asarray(args[1])[0]) if isinstance(args[1], numpy.ndarray) else 0
            image = view.level(0, mip)
            if image is not None:
                out[0][:], out[1][:] = image.width, image.height
                out[2][:] = len(view.images) if view.dimension in ("2darray", "1darray", "3d") else 1
            out[3][:] = view.mips or len(view.images[0]) if view.images else 1
    return tuple(out)


def _system(name):
    def run(m, args, ins, lanes, active):
        value = m.system.get(name)
        if value is None:
            return numpy.zeros(m.n, dtype=_dtype(ins.type))
        value = numpy.asarray(value)
        if value.ndim == 2:
            component = int(numpy.asarray(args[0])[0]) if args else 0
            value = value[:, component]
        return value.astype(_dtype(ins.type)) if _is_float(ins.type) else value.astype(numpy.int64)
    return run


def _discard(m, args, ins, lanes, active):
    m.discarded |= active & numpy.asarray(args[0]).astype(bool)
    return None


def _derivative(axis, fine):
    def run(m, args, ins, lanes, active):
        from .raster import derivative
        return derivative(numpy.asarray(args[0], dtype=numpy.float64), axis, fine).astype(numpy.float32)
    return run


def _calculate_lod(m, args, ins, lanes, active):
    from .gpu import DEFAULT_SAMPLER, view_lod, view_levels
    view = m._view(args[0])
    if view is None:
        return numpy.zeros(m.n, dtype=numpy.float32)
    coords = _coords(args[2:5], _dimensions(view))
    raw = view_lod(view, DEFAULT_SAMPLER, coords)
    if raw is None:
        return numpy.zeros(m.n, dtype=numpy.float32)
    clamped = bool(numpy.asarray(args[5])[0]) if len(args) > 5 else True
    if clamped:
        raw = numpy.clip(raw, 0, len(view_levels(view, 0)) - 1)
    return raw.astype(numpy.float32)


def _unary(function, floats=True):
    def run(m, args, ins, lanes, active):
        a = args[0]
        out = function(a.astype(numpy.float64) if floats and _is_float(ins.type) else a)
        return out.astype(_dtype(ins.type)) if _is_float(ins.type) else numpy.asarray(out).astype(numpy.int64)
    return run


def _binary(function):
    def run(m, args, ins, lanes, active):
        out = function(args[0], args[1], ins.type)
        return out.astype(_dtype(ins.type)) if _is_float(ins.type) else out.astype(numpy.int64) & ((1 << _width(ins.type)) - 1)
    return run


def _tertiary(function):
    def run(m, args, ins, lanes, active):
        out = function(args[0], args[1], args[2], ins.type)
        return out.astype(_dtype(ins.type)) if _is_float(ins.type) else out.astype(numpy.int64) & ((1 << _width(ins.type)) - 1)
    return run


def _dot(count):
    def run(m, args, ins, lanes, active):
        total = sum(args[k].astype(numpy.float64) * args[count + k].astype(numpy.float64) for k in range(count))
        return total.astype(_dtype(ins.type))
    return run


def _countbits(a):
    a = a.astype(numpy.int64) & 0xFFFFFFFF
    out = numpy.zeros_like(a)
    for bit in range(32):
        out += (a >> bit) & 1
    return out


def _firstbit_hi(a):
    a = a.astype(numpy.int64) & 0xFFFFFFFF
    out = numpy.full(a.shape, 0xFFFFFFFF, dtype=numpy.int64)
    for bit in range(32):
        out = numpy.where((a >> bit) & 1, 31 - bit, out)
    return out


def _firstbit_lo(a):
    a = a.astype(numpy.int64) & 0xFFFFFFFF
    out = numpy.full(a.shape, 0xFFFFFFFF, dtype=numpy.int64)
    for bit in range(31, -1, -1):
        out = numpy.where((a >> bit) & 1, bit, out)
    return out


def _firstbit_shi(a):
    s = _signed(a, 32)
    return _firstbit_hi(numpy.where(s < 0, ~s & 0xFFFFFFFF, s))


def _bfrev(a):
    a = a.astype(numpy.int64) & 0xFFFFFFFF
    out = numpy.zeros_like(a)
    for bit in range(32):
        out |= ((a >> bit) & 1) << (31 - bit)
    return out


def _bfe(signed):
    """ibfe / ubfe (width, offset, value)."""
    def f(width, offset, value, t):
        w, o = width.astype(numpy.int64) & 31, offset.astype(numpy.int64) & 31
        v = value.astype(numpy.int64) & 0xFFFFFFFF
        field = (v >> o) & ((1 << w) - 1)
        if signed:
            sign = 1 << numpy.maximum(w - 1, 0)
            field = (field ^ sign) - sign
        whole = (_signed(v, 32) >> o) if signed else (v >> o)
        return numpy.where(w == 0, 0, numpy.where(w + o < 32, field, whole)) & 0xFFFFFFFF
    return f


def _wave_first(m, args, ins, lanes, active):
    wave, _ = _wave_groups(m)
    out = numpy.zeros(m.n, dtype=numpy.int64)
    for w in numpy.unique(wave[active]):
        first = numpy.nonzero(active & (wave == w))[0][0]
        out[first] = 1
    return out


def _wave_groups(m):
    """The wave of each lane and its index in it: lanes in order, 32 by 32, a wave never spanning two thread groups
    (compute, amplification, mesh: by the index of the thread in its group)."""
    lane = numpy.arange(m.n)
    local = m.system.get("flattened")
    if local is not None and len(numpy.asarray(local)) == m.n and m.n:
        local = numpy.asarray(local, dtype=numpy.int64)
        per_group = -(-(int(local.max()) + 1) // WAVE)
        return numpy.asarray(m.groups, dtype=numpy.int64) * per_group + local // WAVE, local % WAVE
    return lane // WAVE, lane % WAVE


def _wave_active_op(m, args, ins, lanes, active):
    value = args[0]
    op = int(numpy.asarray(args[1])[0])
    signed = int(numpy.asarray(args[2])[0]) if len(args) > 2 else 0
    wave, _ = _wave_groups(m)
    out = numpy.zeros(m.n, dtype=_dtype(ins.type))
    for w in numpy.unique(wave[active]):
        sel = active & (wave == w)
        items = value[sel]
        if op == 0:
            result = items.sum()
        elif op == 1:
            result = items.prod()
        elif op == 2:
            result = items.min() if _is_float(ins.type) or signed else (items & 0xFFFFFFFF).min()
        else:
            result = items.max() if _is_float(ins.type) or signed else (items & 0xFFFFFFFF).max()
        out[wave == w] = result
    return out if _is_float(ins.type) else out & ((1 << _width(ins.type)) - 1)


def _wave_any_all(all_):
    def run(m, args, ins, lanes, active):
        wave, _ = _wave_groups(m)
        value = numpy.asarray(args[0]).astype(bool)
        out = numpy.zeros(m.n, dtype=numpy.int64)
        for w in numpy.unique(wave[active]):
            sel = active & (wave == w)
            out[wave == w] = int(value[sel].all() if all_ else value[sel].any())
        return out
    return run


def _wave_read_first(m, args, ins, lanes, active):
    wave, _ = _wave_groups(m)
    out = args[0].copy()
    for w in numpy.unique(wave[active]):
        sel = numpy.nonzero(active & (wave == w))[0]
        out[wave == w] = args[0][sel[0]]
    return out


def _wave_ballot(m, args, ins, lanes, active):
    wave, index = _wave_groups(m)
    value = numpy.asarray(args[0]).astype(bool) & active
    words = [numpy.zeros(m.n, dtype=numpy.int64) for _ in range(4)]
    for w in numpy.unique(wave):
        mask = 0
        for lane in numpy.nonzero(value & (wave == w))[0]:
            mask |= 1 << int(index[lane])
        words[0][wave == w] = mask & 0xFFFFFFFF
    return tuple(words)


def _wave_prefix(m, args, ins, lanes, active):
    value = args[0]
    op = int(numpy.asarray(args[1])[0])
    wave, _ = _wave_groups(m)
    out = numpy.zeros(m.n, dtype=_dtype(ins.type))
    for w in numpy.unique(wave):
        sel = numpy.nonzero(active & (wave == w))[0]
        running = 0 if op == 0 else 1
        for lane in sel:
            out[lane] = running
            running = running + value[lane] if op == 0 else running * value[lane]
    return out


_DX = {
    4: _load_input, 5: _store_output, 57: _create_handle, 59: _cbuffer_legacy,
    60: _sample("sample"), 61: _sample("bias"), 62: _sample("level"), 63: _sample("grad"), 64: _sample("cmp"),
    65: _sample("cmp_lz"), 66: _texture_load, 67: _texture_store, 68: _buffer_load(False), 69: _buffer_store,
    139: _buffer_load(True), 140: _buffer_store, 72: _get_dimensions, 78: _atomic_binop, 79: _atomic_cmpxchg,
    80: lambda m, a, i, l, act: None, 82: _discard, 81: _calculate_lod,
    83: _derivative("x", False), 84: _derivative("y", False), 85: _derivative("x", True), 86: _derivative("y", True),
    93: _system("thread_id"), 94: _system("group_id"), 95: _system("thread_in_group"), 96: _system("flattened"),
    108: _system("primitive_id"), 141: _system("instance_id"),
    6: _unary(numpy.abs), 7: _unary(lambda x: numpy.clip(x, 0, 1)), 8: _unary(numpy.isnan, False), 9: _unary(numpy.isinf, False),
    10: _unary(numpy.isfinite, False), 12: _unary(numpy.cos), 13: _unary(numpy.sin), 14: _unary(numpy.tan),
    15: _unary(numpy.arccos), 16: _unary(numpy.arcsin), 17: _unary(numpy.arctan), 18: _unary(numpy.cosh), 19: _unary(numpy.sinh),
    20: _unary(numpy.tanh), 21: _unary(numpy.exp2), 22: _unary(lambda x: x - numpy.floor(x)), 23: _unary(numpy.log2),
    24: _unary(numpy.sqrt), 25: _unary(lambda x: 1 / numpy.sqrt(x)), 26: _unary(numpy.rint), 27: _unary(numpy.floor),
    28: _unary(numpy.ceil), 29: _unary(numpy.trunc), 30: _unary(_bfrev, False), 31: _unary(_countbits, False),
    32: _unary(_firstbit_lo, False), 33: _unary(_firstbit_hi, False), 34: _unary(_firstbit_shi, False),
    35: _binary(lambda a, b, t: numpy.fmax(a, b)), 36: _binary(lambda a, b, t: numpy.fmin(a, b)),
    37: _binary(lambda a, b, t: numpy.maximum(_signed(a, _width(t)), _signed(b, _width(t)))),
    38: _binary(lambda a, b, t: numpy.minimum(_signed(a, _width(t)), _signed(b, _width(t)))),
    39: _binary(lambda a, b, t: numpy.maximum(_mask(a, _width(t)), _mask(b, _width(t)))),
    40: _binary(lambda a, b, t: numpy.minimum(_mask(a, _width(t)), _mask(b, _width(t)))),
    46: _tertiary(lambda a, b, c, t: a.astype(numpy.float64) * b + c), 47: _tertiary(lambda a, b, c, t: a.astype(numpy.float64) * b + c),
    48: _tertiary(lambda a, b, c, t: _signed(a, 32) * _signed(b, 32) + _signed(c, 32)),
    49: _tertiary(lambda a, b, c, t: _mask(a, 32) * _mask(b, 32) + _mask(c, 32)),
    51: _tertiary(_bfe(True)), 52: _tertiary(_bfe(False)),
    54: _dot(2), 55: _dot(3), 56: _dot(4),
    110: _wave_first,
    111: lambda m, a, i, l, act: _wave_groups(m)[1].astype(numpy.int64),
    112: lambda m, a, i, l, act: numpy.full(m.n, WAVE, dtype=numpy.int64),
    113: _wave_any_all(False), 114: _wave_any_all(True), 116: _wave_ballot, 118: _wave_read_first,
    119: _wave_active_op, 121: _wave_prefix,
    130: _unary(lambda x: x.astype(numpy.float32).astype(numpy.float16).view(numpy.uint16).astype(numpy.int64), False),
    131: _unary(lambda x: (x.astype(numpy.int64) & 0xFFFF).astype(numpy.uint16).view(numpy.float16).astype(numpy.float32), False),
    221: lambda m, a, i, l, act: m.helper.astype(numpy.int64),
}


def _create_handle_from_binding(m, args, ins, lanes, active):
    bind = args[0]          # %dx.types.ResBind {lower, upper, space, class}
    lower, _, space, klass = (int(numpy.asarray(v)[0]) for v in bind)
    index = int(numpy.asarray(args[1])[0])
    return Handle(klass, index if index >= lower else lower + index, space=space)


def _annotate_handle(m, args, ins, lanes, active):
    handle = args[0]
    if isinstance(handle, Handle):
        properties = args[1]
        try:
            word = int(numpy.asarray(properties[0])[0])
            handle.kind = word & 0xFF
            if handle.kind == 12:
                handle.stride = int(numpy.asarray(properties[1])[0])
        except (TypeError, IndexError):
            pass
    return handle


_DX[217] = _create_handle_from_binding
_DX[216] = _annotate_handle


# ------------------------------------------------------------------ the stages
def run_vertex(shader: Shader, n: int, inputs, bindings: Bindings, system=None) -> Result:
    shader.check()
    return Machine(shader, shader.function, n, bindings, inputs=inputs, system=system).run()


def run_geometry(shader: Shader, n: int, inputs2d, bindings: Bindings, primitive_id) -> List[Result]:
    """One run per instance of the geometry shader: its events (emit / cut), as dxbc.run_geometry."""
    shader.check()
    results = []
    for instance in range(shader.gs_instances):
        system = {"primitive_id": primitive_id, "gs_instance": numpy.full(n, instance, dtype=numpy.uint32)}
        results.append(Machine(shader, shader.function, n, bindings, system=system, inputs2d=inputs2d).run())
    return results


def run_hull(shader: Shader, patches: int, vicp, bindings: Bindings, primitive_id):
    """The control point function on every output point, then the patch constant function on every patch:
    (output control points reg -> (p, k, 4), patch constants reg -> (p, 4))."""
    shader.check()
    count = shader.output_cp or shader.input_cp or 1
    lanes = patches * count
    patch_of = numpy.repeat(numpy.arange(patches), count)
    system = {"output_cp_id": numpy.tile(numpy.arange(count), patches).astype(numpy.uint32),
              "primitive_id": numpy.asarray(primitive_id)[patch_of]}
    result = Machine(shader, shader.function, lanes, bindings, system=system,
                     inputs2d={reg: values[patch_of] for reg, values in vicp.items()}).run()
    vocp = {reg: values.reshape(patches, count, 4) for reg, values in result.outputs.items()}
    constants: Dict[int, numpy.ndarray] = {}
    if shader.patch_function is not None:
        machine = Machine(shader, shader.patch_function, patches, bindings, system={"primitive_id": primitive_id},
                          inputs2d=vicp, vocp=vocp)
        machine.patch_outputs = constants
        machine.run()
    return vocp, constants


def run_domain(shader: Shader, n: int, domain, vicp, vpc, bindings: Bindings, primitive_id) -> Result:
    shader.check()
    system = {"domain": numpy.asarray(domain).view(numpy.float32), "primitive_id": primitive_id}
    return Machine(shader, shader.function, n, bindings, system=system, inputs2d=vicp, vpc=vpc).run()


def run_pixel(shader: Shader, n: int, inputs, bindings: Bindings, system=None, helper=None) -> Result:
    shader.check()
    machine = Machine(shader, shader.function, n, bindings, inputs=inputs, system=system, helper=helper)
    result = machine.run()
    result.alive = ~machine.discarded
    return result


def run_compute(shader: Shader, groups, bindings: Bindings, limit=1 << 20):
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
        system = {"thread_id": g * [tx, ty, tz] + t, "group_id": g, "thread_in_group": t,
                  "flattened": numpy.tile(local, count)}
        Machine(shader, shader.function, n, bindings, system=system, groups=numpy.repeat(numpy.arange(count), per_group),
                group_count=count).run()


def needs_quads(shader: Shader, mipmaps=True) -> bool:
    if shader.function is None:
        return False
    wanted = {83, 84, 85, 86, 81} | ({60, 61, 64} if mipmaps else set())
    for block in shader.function.blocks:
        for ins in block:
            if ins.op == "call" and ins.operands:
                callee = shader.function.all_values[ins.extra["callee"]]
                if (callee.name or "").startswith("dx.op."):
                    value = shader.function.all_values[ins.operands[0]]
                    if value.kind == "const" and isinstance(value.value, int) and value.value in wanted:
                        return True
    return False


from . import dxil_ops  # noqa: E402,F401  (the rarer operations, registered in _DX)
from . import dxr  # noqa: E402,F401  (RayQuery and createHandleForLib: dxr.py registers them in _DX)
from . import workgraph  # noqa: E402,F401  (the node operations of work graphs: registered in _DX)
