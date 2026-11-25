# Fathom addition to dumpulator: the work graphs of Direct3D 12 (shader model 6.8) for d3d12_emu.py and dxil.py.
# Boost Software License 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# A work graph is a program of a state object (D3D12_STATE_OBJECT_TYPE_EXECUTABLE, a WORK_GRAPH subobject): the
# node shaders of its DXIL libraries (their node ID, launch: broadcasting, coalescing or thread, their input and
# outputs, from the entry points' metadata). DispatchGraph puts the CPU's (or GPU's) records in the queue of an
# entry node; then, as long as a node has records, they run: broadcasting, a dispatch grid per record (fixed or
# the record's SV_DispatchGrid) whose groups share it; coalescing, groups of up to MaxRecords records; thread,
# one record per thread. The records a node writes (AllocateNodeOutputRecords per thread or per group,
# GetNodeRecordPtr, OutputComplete; IncrementOutputCount for empty records) go to the queue of the node its
# output (or output array element) names. Records are bytes between nodes, laid out as HLSL lays out their
# structure, and shader memory (dxil.Memory) while a node reads or writes them.

import struct
from collections import OrderedDict
from typing import Dict, List

import numpy

from .dxil import Memory, Pointer

BROADCASTING, COALESCING, THREAD = 1, 2, 3


def _layout(t) -> List[tuple]:
    """(offset, scalar type) of the scalars of a record type, in the order of dxil.Memory's cells; and its size."""
    out = []

    def walk(t, base):
        if t.kind == "struct":
            offset = base
            for e in t.elements:
                align = _align(e)
                offset += -(offset - base) % align
                offset = walk(e, offset)
            return offset
        if t.kind in ("array", "vector"):
            offset = base
            for _ in range(t.count):
                offset = walk(t.elements[0], offset)
            return offset
        size = max(1, (t.bits or 32) // 8) if t.kind in ("int", "float", "half", "double") else 4
        if t.kind == "int" and t.bits == 1:
            size = 4
        out.append((base, t, size))
        return base + size
    total = walk(t, 0)
    align = _align(t)
    total += -total % align
    return out, total


def _align(t) -> int:
    if t.kind == "struct":
        return max([_align(e) for e in t.elements] or [4])
    if t.kind in ("array", "vector"):
        return _align(t.elements[0])
    return max(1, min(8, (t.bits or 32) // 8)) if t.bits != 1 else 4


def to_memory(t, records: List[bytes]) -> Memory:
    """A memory of rows = the records (bytes), cells = the scalars of the type."""
    memory = Memory(t, max(1, len(records)))
    layout, size = _layout(t)
    for k, (offset, scalar, width) in enumerate(layout):
        kind = {1: "<B", 2: "<H", 4: "<I", 8: "<Q"}[width]
        raw = numpy.zeros(len(records), dtype=numpy.int64)
        for r, data in enumerate(records):
            if offset + width <= len(data):
                raw[r] = struct.unpack_from(kind, data, offset)[0]
        if scalar.kind in ("float", "half", "double"):
            view = {2: numpy.float16, 4: numpy.float32, 8: numpy.float64}[width]
            values = raw.astype({2: numpy.uint16, 4: numpy.uint32, 8: numpy.uint64}[width]).view(view)
            memory.cells[k][:len(records)] = values.astype(memory.cells[k].dtype)
        else:
            memory.cells[k][:len(records)] = raw
    return memory


def from_memory(memory: Memory, t, rows, size: int) -> List[bytes]:
    """The records (bytes) of rows of a memory of a record type."""
    layout, natural = _layout(t)
    size = max(size, natural)
    out = []
    for row in rows:
        data = bytearray(size)
        for k, (offset, scalar, width) in enumerate(layout):
            if k >= len(memory.cells) or offset + width > size:
                continue
            value = memory.cells[k][row]
            if scalar.kind in ("float", "half", "double"):
                kind = {2: "<e", 4: "<f", 8: "<d"}[width]
                struct.pack_into(kind, data, offset, float(value))
            else:
                struct.pack_into({1: "<B", 2: "<H", 4: "<I", 8: "<Q"}[width], data, offset, int(value) & ((1 << (8 * width)) - 1))
        out.append(bytes(data))
    return out


# ------------------------------------------------------------------ the nodes of a library
def _pairs(shader, items):
    return {shader._int(items[k]): items[k + 1] for k in range(0, len(items) - 1, 2)} if isinstance(items, list) else {}


def _io(shader, item):
    """An input or output of a node: flags, record size, max records, output ID, array size, the dispatch grid field."""
    tags = _pairs(shader, item)
    record = _pairs(shader, tags.get(2))
    io = dict(flags=shader._int(tags.get(1, 0)), size=shader._int(record.get(0, 0)), max_records=shader._int(tags.get(3, 1)) or 1,
              array=shader._int(tags.get(5, 1)) or 1, target=None, grid=None)
    if isinstance(tags.get(0), list) and tags[0]:
        name = tags[0][0] if isinstance(tags[0][0], str) else ""
        io["target"] = (name, shader._int(tags[0][1]) if len(tags[0]) > 1 else 0)
    grid = record.get(1)
    if isinstance(grid, list) and len(grid) >= 3:
        io["grid"] = tuple(shader._int(v) for v in grid[:3])      # byte offset, component type, components
    return io


class Node:
    def __init__(self, shader, name, function, properties):
        tags = _pairs(shader, properties)
        self.shader, self.function, self.name = shader, function, name
        self.launch = shader._int(tags.get(13, BROADCASTING))
        self.entry = bool(shader.module.constant(tags.get(14))) if tags.get(14) is not None else False
        ident = tags.get(15)
        self.id = (ident[0], shader._int(ident[1])) if isinstance(ident, list) and ident else (name, 0)
        threads = tags.get(4)
        self.threads = tuple(shader._int(v) for v in threads[:3]) if isinstance(threads, list) else (1, 1, 1)
        grid = tags.get(18)
        self.grid = tuple(shader._int(v) for v in grid[:3]) if isinstance(grid, list) else None
        self.inputs = [_io(shader, item) for item in (tags.get(20) or [])]
        self.outputs = [_io(shader, item) for item in (tags.get(21) or [])]
        self.recursion = shader._int(tags.get(19, 0))
        self.table_index = shader._int(tags.get(16, -1))

    @property
    def record_size(self):
        return self.inputs[0]["size"] if self.inputs else 0


def library_nodes(shader) -> List[Node]:
    nodes = []
    for name, (function, signatures, properties) in shader.entries.items():
        if function is None or not isinstance(properties, list):
            continue
        tags = _pairs(shader, properties)
        if shader._int(tags.get(8, -1)) == 15:          # shader kind: node
            nodes.append(Node(shader, name, function, properties))
    return nodes


class WorkGraph:
    """A work graph program: its nodes by ID, its entry points (in their index order)."""

    def __init__(self, name, nodes: List[Node], entrypoints=None):
        self.name = name
        self.nodes: Dict[tuple, Node] = OrderedDict((n.id, n) for n in nodes)
        self.order = list(self.nodes)
        entry_ids = entrypoints or [n.id for n in nodes if n.entry]
        self.entrypoints = [i for i in entry_ids if i in self.nodes]

    def memory_size(self):
        return 64 * 1024


# ------------------------------------------------------------------ running
class InputRecords:
    """The input records of the lanes of a node: rows (per lane: the first record), counts."""

    def __init__(self, records: List[bytes], base, count, size):
        self.records, self.base, self.count, self.size = records, base, count, size
        self.memory = None
        self.type = None

    def pointer(self, t, index, n):
        if self.memory is None or self.type is not t:
            self.memory, self.type = to_memory(t, self.records), t
        rows = numpy.clip(self.base + numpy.asarray(index, dtype=numpy.int64), 0, max(0, len(self.records) - 1))
        return Pointer(self.memory, numpy.zeros(n, dtype=numpy.int64), rows)


class OutputNode:
    """A NodeOutput (or NodeOutputArray) handle: the output's description, the array index of each lane."""

    def __init__(self, io, index=None):
        self.io, self.index = io, index


class OutputRecords:
    """Records a node allocated: per lane its first row and count, the target of each lane."""

    def __init__(self, graph, io, n, rows, base, count, targets, per_thread, groups):
        self.graph, self.io, self.base, self.count, self.targets = graph, io, base, count, targets
        self.per_thread, self.groups, self.rows = per_thread, groups, rows
        self.memory = None
        self.type = None
        self.done = False

    def pointer(self, t, index, n):
        if self.memory is None:
            self.memory, self.type = Memory(t, max(1, self.rows)), t
        rows = numpy.clip(self.base + numpy.asarray(index, dtype=numpy.int64), 0, max(0, self.rows - 1))
        return Pointer(self.memory, numpy.zeros(n, dtype=numpy.int64), rows)

    def complete(self, active):
        if self.done:
            return
        self.done = True
        size = self.io["size"]
        lanes = numpy.nonzero(active)[0]
        seen = set()
        for lane in lanes:
            key = lane if self.per_thread else int(self.groups[lane])
            if key in seen:
                continue
            seen.add(key)
            rows = range(int(self.base[lane]), int(self.base[lane]) + int(self.count[lane]))
            if self.memory is not None:
                records = from_memory(self.memory, self.type, rows, size)
            else:
                records = [bytes(size)] * len(rows)
            self.graph.enqueue(self.targets[lane], records)


class Run:
    """The records waiting in the queues of the nodes of a graph, until none is left."""

    def __init__(self, graph: WorkGraph, bindings, limit=1 << 16):
        self.graph, self.bindings = graph, bindings
        self.queues: Dict[tuple, List[bytes]] = OrderedDict((i, []) for i in graph.order)
        self.limit = limit

    def enqueue(self, target, records):
        if target in self.queues:
            self.queues[target].extend(records)

    def run(self):
        launches = 0
        while launches < self.limit:
            node_id = next((i for i, q in self.queues.items() if q), None)
            if node_id is None:
                break
            records, self.queues[node_id] = self.queues[node_id], []
            self._node(self.graph.nodes[node_id], records)
            launches += 1

    def _node(self, node: Node, records: List[bytes]):
        shader = node.shader
        saved = shader.thread_group
        shader.thread_group = node.threads if node.launch != THREAD else (1, 1, 1)
        try:
            if node.launch == BROADCASTING:
                for record in records:
                    grid = node.grid or self._grid(node, record)
                    self._groups(node, grid, lambda count, per: InputRecords([record], numpy.zeros(count * per, dtype=numpy.int64),
                                                                            numpy.ones(count * per, dtype=numpy.int64), node.record_size))
            elif node.launch == COALESCING:
                chunk = node.inputs[0]["max_records"] if node.inputs else 1
                groups = [records[k:k + chunk] for k in range(0, len(records), chunk)] or ([[]] if not node.inputs else [])
                for start in range(0, len(groups), 4096):
                    part = groups[start:start + 4096]
                    flat = [r for g in part for r in g]
                    starts = numpy.cumsum([0] + [len(g) for g in part[:-1]])
                    counts = numpy.array([len(g) for g in part])
                    self._groups(node, (len(part), 1, 1),
                                 lambda count, per, s=starts, c=counts, f=flat: InputRecords(f, numpy.repeat(s, per),
                                                                                             numpy.repeat(c, per), node.record_size))
            else:
                for start in range(0, len(records), 1 << 16):
                    part = records[start:start + (1 << 16)]
                    self._groups(node, (len(part), 1, 1),
                                 lambda count, per, p=part: InputRecords(p, numpy.arange(count), numpy.ones(count, dtype=numpy.int64),
                                                                         node.record_size))
        finally:
            shader.thread_group = saved

    def _grid(self, node, record):
        field = node.inputs[0].get("grid") if node.inputs else None
        if field is None:
            return (1, 1, 1)
        offset, kind, count = field
        width = 2 if kind in (6, 7) else 4         # 16 or 32-bit components
        values = [struct.unpack_from("<H" if width == 2 else "<I", record, offset + width * k)[0]
                  if offset + width * (k + 1) <= len(record) else 1 for k in range(count)]
        return tuple(values + [1] * (3 - len(values)))

    def _groups(self, node, grid, make_input):
        from .dxil import Machine
        from .dxil_ops import _groups

        def batch(ids, n, system, lane_group, count):
            per = n // max(1, count)
            machine = Machine(node.shader, node.function, n, self.bindings, system=system, groups=lane_group, group_count=count)
            machine.graph = (self, node, make_input(count, per))
            machine.run()
        _groups(node.shader, grid, batch)


# ------------------------------------------------------------------ the operations (dxil._DX)
def _graph(m):
    return getattr(m, "graph", None)


def _create_input(m, args, ins, lanes, active):
    context = _graph(m)
    return context[2] if context is not None else None


def _create_output(m, args, ins, lanes, active):
    context = _graph(m)
    if context is None:
        return None
    index = int(numpy.asarray(args[0]).ravel()[0])
    outputs = context[1].outputs
    return OutputNode(outputs[index] if 0 <= index < len(outputs) else dict(size=0, target=None, array=1), None)


def _index_node(m, args, ins, lanes, active):
    handle = args[0]
    if isinstance(handle, OutputNode):
        return OutputNode(handle.io, numpy.broadcast_to(numpy.asarray(args[1], dtype=numpy.int64), (m.n,)).copy())
    return handle


def _annotate(m, args, ins, lanes, active):
    return args[0]


def _targets(io, index, n):
    if io.get("target") is None:
        return [None] * n
    name, first = io["target"]
    index = numpy.zeros(n, dtype=numpy.int64) if index is None else index
    return [(name, first + int(i)) for i in index]


def _allocate(m, args, ins, lanes, active):
    context = _graph(m)
    handle = args[0]
    if context is None or not isinstance(handle, OutputNode):
        return None
    count = numpy.broadcast_to(numpy.asarray(args[1], dtype=numpy.int64), (m.n,))
    per_thread = bool(int(numpy.asarray(args[2]).ravel()[0]))
    groups = numpy.asarray(m.groups, dtype=numpy.int64)
    base = numpy.zeros(m.n, dtype=numpy.int64)
    counts = numpy.where(active, count, 0)
    if per_thread:
        base[:] = numpy.cumsum(counts) - counts
        rows = int(counts.sum())
    else:
        per_group = {}
        rows = 0
        for lane in numpy.nonzero(active)[0]:
            g = int(groups[lane])
            if g not in per_group:
                per_group[g] = (rows, int(count[lane]))
                rows += int(count[lane])
        for lane in range(m.n):
            base[lane], counts[lane] = per_group.get(int(groups[lane]), (0, 0))
    return OutputRecords(context[0], handle.io, m.n, rows, base, counts, _targets(handle.io, handle.index, m.n), per_thread, groups)


def _record_pointer(m, args, ins, lanes, active):
    handle = args[0]
    if isinstance(handle, (InputRecords, OutputRecords)):
        return handle.pointer(ins.type.pointee, args[1], m.n)
    return Pointer(Memory(ins.type.pointee, m.n), numpy.zeros(m.n, dtype=numpy.int64), numpy.arange(m.n))


def _complete(m, args, ins, lanes, active):
    if isinstance(args[0], OutputRecords):
        args[0].complete(active)
    return None


def _increment(m, args, ins, lanes, active):
    """IncrementOutputCount (EmptyNodeOutput): empty records for the target."""
    context = _graph(m)
    handle = args[0]
    if context is None or not isinstance(handle, OutputNode):
        return None
    count = numpy.broadcast_to(numpy.asarray(args[1], dtype=numpy.int64), (m.n,))
    per_thread = bool(int(numpy.asarray(args[2]).ravel()[0]))
    targets = _targets(handle.io, handle.index, m.n)
    seen = set()
    for lane in numpy.nonzero(active)[0]:
        key = lane if per_thread else int(m.groups[lane])
        if key in seen:
            continue
        seen.add(key)
        context[0].enqueue(targets[lane], [bytes(handle.io.get("size", 0))] * int(count[lane]))
    return None


def _input_count(m, args, ins, lanes, active):
    handle = args[0]
    if isinstance(handle, InputRecords):
        return numpy.broadcast_to(handle.count, (m.n,)).astype(numpy.int64)
    return numpy.zeros(m.n, dtype=numpy.int64)


def _output_valid(m, args, ins, lanes, active):
    context = _graph(m)
    handle = args[0]
    out = numpy.zeros(m.n, dtype=numpy.int64)
    if context is not None and isinstance(handle, OutputNode):
        for lane, target in enumerate(_targets(handle.io, handle.index, m.n)):
            out[lane] = int(target in context[0].queues)
    return out


def _finished_sharing(m, args, ins, lanes, active):
    groups = numpy.asarray(m.groups, dtype=numpy.int64)
    return (groups == groups.max()).astype(numpy.int64) if m.n else numpy.zeros(0, dtype=numpy.int64)


def _nothing(m, args, ins, lanes, active):
    return None


def _remaining_recursion(m, args, ins, lanes, active):
    context = _graph(m)
    return numpy.full(m.n, context[1].recursion if context is not None else 0, dtype=numpy.int64)


OPERATIONS = {238: _allocate, 239: _record_pointer, 240: _increment, 241: _complete, 242: _input_count, 243: _finished_sharing,
              244: _nothing, 245: _nothing, 246: _nothing, 247: _create_output, 248: _index_node, 249: _annotate,
              250: _create_input, 251: _annotate, 252: _output_valid, 253: _remaining_recursion}


def _register():
    from .dxil import _DX
    _DX.update(OPERATIONS)


_register()
