# Fathom addition to dumpulator: the ray tracing of Direct3D 12 (DXR 1.0 and 1.1) for d3d12_emu.py. Boost
# Software License 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# State objects: DXIL libraries (their exports, renamed or not, and the subobjects they declare in their RDAT
# part), existing collections, hit groups (triangles or procedural), global and local root signatures with
# their associations to exports, shader and pipeline configurations. Acceleration structures: bottom level
# (triangles, indexed or not, with a transform; AABBs) and top level (instances: transform, InstanceID, mask,
# hit group contribution, flags), copied as they are built (CopyRaytracingAccelerationStructure too).
#
# DispatchRays runs the ray generation shader on all the rays at once. TraceRay intersects the rays of the
# active lanes with every triangle (numpy, Moller-Trumbore in object space) and every AABB (slabs) of the
# instances its mask selects; the candidates are taken nearest first: an opaque triangle commits, a non
# opaque one asks its any hit shader (IgnoreHit, AcceptHitAndEndSearch), an AABB runs the intersection
# shader of its hit group, whose ReportHit calls the any hit shader (non opaque geometry) and commits with
# the attributes it gives; ray flags (culling, opaque forcing, skipping triangles or procedural primitives,
# accepting the first hit, skipping the closest hit shader) apply. Then the closest hit shader of each
# lane's hit group runs (or the miss shader of the miss index), with the caller's payload (shared memory,
# same lanes) and the committed attributes. Each shader record's local root arguments (after its 32-byte
# identifier) are bound for its shaders, as its export's local root signature lays them out.
#
# RayQuery (inline ray tracing, in any shader): TraceRayInline gathers the candidates of each lane, nearest
# first; Proceed commits the opaque triangles and stops at each candidate the shader must decide on (a non
# opaque triangle, a procedural primitive); the committed and candidate values are read as the shader asks.

import struct
from typing import Dict, List, Optional

import numpy

from .dxil import Handle, Machine, Memory, Pointer
from .dxil_bitcode import Type

RAY_FLAG_FORCE_OPAQUE, RAY_FLAG_FORCE_NON_OPAQUE = 0x1, 0x2
RAY_FLAG_ACCEPT_FIRST_HIT, RAY_FLAG_SKIP_CLOSEST_HIT = 0x4, 0x8
RAY_FLAG_CULL_BACK, RAY_FLAG_CULL_FRONT, RAY_FLAG_CULL_OPAQUE, RAY_FLAG_CULL_NON_OPAQUE = 0x10, 0x20, 0x40, 0x80
RAY_FLAG_SKIP_TRIANGLES, RAY_FLAG_SKIP_PROCEDURAL = 0x100, 0x200
HIT_KIND_FRONT, HIT_KIND_BACK = 254, 255
INSTANCE_CULL_DISABLE, INSTANCE_FRONT_CCW, INSTANCE_FORCE_OPAQUE, INSTANCE_FORCE_NON_OPAQUE = 1, 2, 4, 8
GEOMETRY_OPAQUE = 1


class Structure:
    """An acceleration structure: bottom (geometries: kind "triangles" (k, 3, 3) or "aabbs" (k, 2, 3), flags) or
    top (instances)."""

    def __init__(self, kind):
        self.kind = kind
        self.geometries: List[dict] = []
        self.instances: List[dict] = []


# ------------------------------------------------------------------ state objects
class StateObject:
    """ID3D12StateObject / ID3D12StateObjectProperties: the exports (name -> ("shader", library, function) or
    ("hitgroup", dict)), their identifiers, their local root signatures."""
    interface = "ID3D12StateObject"
    interfaces = ["ID3D12StateObjectProperties", "ID3D12StateObjectProperties1", "ID3D12WorkGraphProperties"]

    def __init__(self, d3d):
        self.d3d = d3d
        self.exports: Dict[str, tuple] = {}
        self.identifiers: Dict[bytes, str] = {}
        self.addresses: Dict[str, int] = {}
        self.global_root = None
        self.local_roots: Dict[str, object] = {}    # export -> its local root signature
        self.default_local_root = None
        self.resources: Dict[int, dict] = {}        # the declared resources of its libraries (root SRV/UAV strides)
        self.uavs: Dict[int, dict] = {}
        self.payload_size = self.attribute_size = 0
        self.recursion = 1
        self.libraries = []                         # the DXIL libraries (their node shaders)
        self.graphs = []                            # the work graph programs (workgraph.WorkGraph)
        self.strings: Dict[str, int] = {}           # names given back to the program, in its memory

    def identifier(self, name: str) -> int:
        if name not in self.exports:
            return 0
        if name not in self.addresses:
            ident = b"FATHOMRT" + struct.pack("<II", id(self) & 0xFFFFFFFF, len(self.addresses)) + bytes(16)
            dp = self.d3d.dp
            address = dp.allocate(32)
            dp.write(address, ident)
            self.addresses[name] = address
            self.identifiers[ident] = name
        return self.addresses[name]

    def GetShaderIdentifier(self, name):
        return self.identifier(_wide(self.d3d.dp, name))

    # ID3D12StateObjectProperties1, ID3D12WorkGraphProperties -----------------------------------------
    def GetProgramIdentifier(self, out, name):
        """The identifier of a program (a work graph): 32 bytes, returned in out."""
        program = _wide(self.d3d.dp, name)
        index = next((k for k, g in enumerate(self.graphs) if g.name == program), None)
        ident = bytes(32)
        if index is not None:
            ident = b"FATHOMWG" + struct.pack("<II", id(self) & 0xFFFFFFFF, index) + bytes(16)
            self.d3d.programs[ident] = (self, self.graphs[index])
        self.d3d.dp.write(out, ident)
        return out

    def _text(self, text):
        if text not in self.strings:
            data = (text + "\0").encode("utf-16-le")
            address = self.d3d.dp.allocate(len(data))
            self.d3d.dp.write(address, data)
            self.strings[text] = address
        return self.strings[text]

    def _node_id(self, value):
        """A D3D12_NODE_ID passed by value (x64: the address of a copy; x86: its 8 bytes)."""
        dp = self.d3d.dp
        if dp.ptr_size() == 8:
            name, index = dp.read_ptr(value), struct.unpack("<I", bytes(dp.read(value + 8, 4)))[0]
        else:
            name, index = value & 0xFFFFFFFF, value >> 32
        return _wide(dp, name), index

    def _write_node_id(self, out, node_id):
        dp = self.d3d.dp
        dp.write_ptr(out, self._text(node_id[0]))
        dp.write(out + dp.ptr_size(), struct.pack("<I", node_id[1]))
        return out

    def _graph(self, index):
        return self.graphs[index] if 0 <= index < len(self.graphs) else None

    def GetNumWorkGraphs(self):
        return len(self.graphs)

    def GetProgramName(self, index):
        graph = self._graph(index)
        return self._text(graph.name) if graph is not None else 0

    def GetWorkGraphIndex(self, name):
        program = _wide(self.d3d.dp, name)
        return next((k for k, g in enumerate(self.graphs) if g.name == program), 0xFFFFFFFF)

    def GetNumNodes(self, index):
        graph = self._graph(index)
        return len(graph.order) if graph is not None else 0

    def GetNodeID(self, out, index, node):
        graph = self._graph(index)
        return self._write_node_id(out, graph.order[node] if graph is not None and node < len(graph.order) else ("", 0))

    def GetNodeIndex(self, index, node_id):
        graph = self._graph(index)
        key = self._node_id(node_id)
        return graph.order.index(key) if graph is not None and key in graph.order else 0xFFFFFFFF

    def GetNodeLocalRootArgumentsTableIndex(self, index, node):
        graph = self._graph(index)
        if graph is None or node >= len(graph.order):
            return 0xFFFFFFFF
        return graph.nodes[graph.order[node]].table_index & 0xFFFFFFFF

    def GetNumEntrypoints(self, index):
        graph = self._graph(index)
        return len(graph.entrypoints) if graph is not None else 0

    def GetEntrypointID(self, out, index, entry):
        graph = self._graph(index)
        return self._write_node_id(out, graph.entrypoints[entry] if graph is not None and entry < len(graph.entrypoints) else ("", 0))

    def GetEntrypointIndex(self, index, node_id):
        graph = self._graph(index)
        key = self._node_id(node_id)
        return graph.entrypoints.index(key) if graph is not None and key in graph.entrypoints else 0xFFFFFFFF

    def GetEntrypointRecordSizeInBytes(self, index, entry):
        graph = self._graph(index)
        if graph is None or entry >= len(graph.entrypoints):
            return 0
        return graph.nodes[graph.entrypoints[entry]].record_size

    def GetEntrypointRecordAlignmentInBytes(self, index, entry):
        return 4

    def GetWorkGraphMemoryRequirements(self, index, out):
        graph = self._graph(index)
        size = graph.memory_size() if graph is not None else 0
        self.d3d.dp.write(out, struct.pack("<3Q", size, size, 256))

    def GetShaderStackSize(self, name):
        return 64

    def GetPipelineStackSize(self):
        return 1024

    def SetPipelineStackSize(self, size):
        return None

    def GetDevice(self, riid, out):
        return self.d3d.device_query(riid, out)

    def SetName(self, name):
        return 0

    def SetPrivateData(self, guid, size, data):
        return 0

    def SetPrivateDataInterface(self, guid, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return 0x887A0002

    def lookup(self, data: bytes):
        """The export name of a shader record (its identifier), or None."""
        return self.identifiers.get(bytes(data[:32]))

    def local_root(self, name):
        """The local root signature of an export (a hit group's, or that of one of its shaders), or the default."""
        if name in self.local_roots:
            return self.local_roots[name]
        export = self.exports.get(name)
        if export is not None and export[0] == "hitgroup":
            for shader in (export[1].get("closest"), export[1].get("anyhit"), export[1].get("intersection")):
                if shader and shader in self.local_roots:
                    return self.local_roots[shader]
        return self.default_local_root


def _wide(dp, address) -> str:
    if not address:
        return ""
    out = bytearray()
    while len(out) < 1024:
        pair = bytes(dp.read(address + len(out), 2))
        if pair == b"\0\0":
            break
        out += pair
    return out.decode("utf-16-le", errors="replace")


def _wide_list(dp, address, count):
    ptr = dp.ptr_size()
    return [_wide(dp, dp.read_ptr(address + ptr * k)) for k in range(count)] if address else []


def create_state_object(d3d, desc_address, base: Optional[StateObject] = None) -> StateObject:
    """A state object from a D3D12_STATE_OBJECT_DESC (base: the state object AddToStateObject grows)."""
    from .d3d12_emu import RootSignature
    from .dxil import Shader
    dp = d3d.dp
    ptr = dp.ptr_size()
    kind, count = struct.unpack("<2I", bytes(dp.read(desc_address, 8)))
    subobjects = dp.read_ptr(desc_address + 8)
    state = StateObject(d3d)
    if base is not None:
        state.exports.update(base.exports)
        state.local_roots.update(base.local_roots)
        state.default_local_root, state.global_root = base.default_local_root, base.global_root
        state.resources.update(base.resources)
        state.uavs.update(base.uavs)
        state.identifiers.update(base.identifiers)
        state.addresses.update(base.addresses)
    size = 2 * ptr if ptr == 8 else 8
    hitgroups, associations, roots, work_graphs = [], [], {}, []
    for k in range(count):
        base_address = subobjects + k * size
        sub_type = struct.unpack("<I", bytes(dp.read(base_address, 4)))[0]
        desc = dp.read_ptr(base_address + (8 if ptr == 8 else 4))
        if sub_type == 5:       # DXIL_LIBRARY: bytecode, exports
            code, length = dp.read_ptr(desc), dp.read_ptr(desc + ptr)
            exports = struct.unpack("<I", bytes(dp.read(desc + 2 * ptr, 4)))[0]
            table = dp.read_ptr(desc + 3 * ptr if ptr == 8 else desc + 12)
            library = Shader(bytes(dp.read(code, length)))
            state.libraries.append(library)
            state.resources.update(library.resources)
            state.uavs.update(library.uavs)
            names = {}
            for e in range(exports):
                entry = table + e * (3 * ptr if ptr == 8 else 12)
                name = _wide(dp, dp.read_ptr(entry))
                renamed = _wide(dp, dp.read_ptr(entry + ptr))
                names[name] = renamed or name
            functions = _library_exports(library)
            if not names:
                for export, function in functions.items():
                    state.exports[export] = ("shader", library, function)
            for name, source in names.items():
                if source in functions:
                    state.exports[name] = ("shader", library, functions[source])
            for sub in library_subobjects(library.parts.get(b"RDAT") if hasattr(library, "parts") else None):
                if sub["kind"] == 11:
                    hitgroups.append((sub["name"], dict(type=sub["type"], anyhit=sub["anyhit"] or None,
                                                        closest=sub["closest"] or None,
                                                        intersection=sub["intersection"] or None)))
                elif sub["kind"] in (1, 2):
                    root = RootSignature(d3d, sub["data"])
                    roots[sub["name"]] = (sub["kind"], root)
                    if sub["kind"] == 1:
                        state.global_root = state.global_root or root
                elif sub["kind"] == 8:
                    associations.append((sub["subobject"], sub["exports"]))
                elif sub["kind"] == 9:
                    state.payload_size, state.attribute_size = sub["payload"], sub["attributes"]
                elif sub["kind"] in (10, 12):
                    state.recursion = sub["recursion"]
        elif sub_type == 6:     # EXISTING_COLLECTION: a state object, exports
            collection = d3d.com.impl(dp.read_ptr(desc))
            count_exports = struct.unpack("<I", bytes(dp.read(desc + ptr, 4)))[0]
            if isinstance(collection, StateObject):
                table = dp.read_ptr(desc + 2 * ptr if ptr == 8 else desc + 8)
                wanted = {}
                for e in range(count_exports):
                    entry = table + e * (3 * ptr if ptr == 8 else 12)
                    name = _wide(dp, dp.read_ptr(entry))
                    wanted[name] = _wide(dp, dp.read_ptr(entry + ptr)) or name
                for name, export in collection.exports.items():
                    if not wanted or name in wanted.values():
                        target = next((k for k, v in wanted.items() if v == name), name)
                        state.exports[target] = export
                state.local_roots.update(collection.local_roots)
                state.default_local_root = state.default_local_root or collection.default_local_root
                state.global_root = state.global_root or collection.global_root
                state.resources.update(collection.resources)
                state.uavs.update(collection.uavs)
        elif sub_type == 11:    # HIT_GROUP: export, type, any hit, closest hit, intersection
            name = _wide(dp, dp.read_ptr(desc))
            group_type = struct.unpack("<I", bytes(dp.read(desc + ptr, 4)))[0]
            anyhit = _wide(dp, dp.read_ptr(desc + 2 * ptr))
            closest = _wide(dp, dp.read_ptr(desc + 3 * ptr))
            intersection = _wide(dp, dp.read_ptr(desc + 4 * ptr))
            hitgroups.append((name, dict(type=group_type, anyhit=anyhit or None, closest=closest or None,
                                         intersection=intersection or None)))
        elif sub_type == 1:     # GLOBAL_ROOT_SIGNATURE
            state.global_root = d3d.com.impl(dp.read_ptr(desc))
            roots[base_address] = (1, state.global_root)
        elif sub_type == 2:     # LOCAL_ROOT_SIGNATURE
            roots[base_address] = (2, d3d.com.impl(dp.read_ptr(desc)))
        elif sub_type == 7:     # SUBOBJECT_TO_EXPORTS_ASSOCIATION: the subobject (its address), the exports
            target = dp.read_ptr(desc)
            count_exports = struct.unpack("<I", bytes(dp.read(desc + ptr, 4)))[0]
            associations.append((target, _wide_list(dp, dp.read_ptr(desc + 2 * ptr if ptr == 8 else desc + 8), count_exports)))
        elif sub_type == 8:     # DXIL_SUBOBJECT_TO_EXPORTS_ASSOCIATION: the subobject (its name), the exports
            target = _wide(dp, dp.read_ptr(desc))
            count_exports = struct.unpack("<I", bytes(dp.read(desc + ptr, 4)))[0]
            associations.append((target, _wide_list(dp, dp.read_ptr(desc + 2 * ptr if ptr == 8 else desc + 8), count_exports)))
        elif sub_type == 13:    # WORK_GRAPH: program name, flags, entry points, nodes (all the libraries' nodes)
            name = _wide(dp, dp.read_ptr(desc))
            entries = struct.unpack("<I", bytes(dp.read(desc + (12 if ptr == 8 else 8), 4)))[0]
            table = dp.read_ptr(desc + (16 if ptr == 8 else 12))
            entrypoints = []
            for e in range(entries):
                at = table + e * (16 if ptr == 8 else 8)
                entrypoints.append((_wide(dp, dp.read_ptr(at)), struct.unpack("<I", bytes(dp.read(at + ptr, 4)))[0]))
            work_graphs.append((name, entrypoints))
        elif sub_type == 9:     # RAYTRACING_SHADER_CONFIG
            state.payload_size, state.attribute_size = struct.unpack("<2I", bytes(dp.read(desc, 8)))
        elif sub_type in (10, 12):
            state.recursion = struct.unpack("<I", bytes(dp.read(desc, 4)))[0]
    for name, group in hitgroups:
        state.exports[name] = ("hitgroup", group)
    if work_graphs:
        from .workgraph import WorkGraph, library_nodes
        nodes = [node for library in state.libraries for node in library_nodes(library)]
        for name, entrypoints in work_graphs:
            state.graphs.append(WorkGraph(name, nodes, entrypoints or None))
    # the local root signatures: associated with exports, or the default (an association without exports, or
    # a single local root signature without association)
    associated = set()
    for target, exports in associations:
        found = roots.get(target)
        if found is None or found[0] != 2:
            continue
        associated.add(target)
        if exports:
            for name in exports:
                state.local_roots[name] = found[1]
        else:
            state.default_local_root = found[1]
    unassociated = [r for key, (k, r) in roots.items() if k == 2 and key not in associated]
    if state.default_local_root is None and len(unassociated) == 1:
        state.default_local_root = unassociated[0]
    return state


def library_subobjects(rdat: Optional[bytes]) -> List[dict]:
    """The subobjects a DXIL library declares (its RDAT part: the subobject table, the strings, the raw bytes)."""
    if not rdat or len(rdat) < 8:
        return []
    count = struct.unpack_from("<I", rdat, 4)[0]
    parts = {}
    for k in range(count):
        offset = struct.unpack_from("<I", rdat, 8 + 4 * k)[0]
        kind, size = struct.unpack_from("<2I", rdat, offset)
        parts[kind] = rdat[offset + 8:offset + 8 + size]
    strings, indices, raw = parts.get(1, b""), parts.get(2, b""), parts.get(5, b"")
    table = parts.get(6)
    if not table:
        return []

    def text(at):
        end = strings.find(b"\0", at)
        return strings[at:end if end >= 0 else len(strings)].decode("utf-8", errors="replace")

    def text_array(at):
        if at == 0xFFFFFFFF or 4 * at + 4 > len(indices):
            return []
        n = struct.unpack_from("<I", indices, 4 * at)[0]
        return [text(v) for v in struct.unpack_from(f"<{n}I", indices, 4 * at + 4)]

    records, stride = struct.unpack_from("<2I", table, 0)
    out = []
    for k in range(records):
        words = struct.unpack_from(f"<{stride // 4}I", table, 8 + k * stride) + (0,) * 8
        kind, name = words[0], text(words[1])
        sub = dict(kind=kind, name=name)
        if kind in (1, 2):
            sub["data"] = raw[words[2]:words[2] + words[3]]
        elif kind == 8:
            sub["subobject"], sub["exports"] = text(words[2]), text_array(words[3])
        elif kind == 9:
            sub["payload"], sub["attributes"] = words[2], words[3]
        elif kind in (10, 12):
            sub["recursion"] = words[2]
        elif kind == 11:
            sub.update(type=words[2], anyhit=text(words[3]), closest=text(words[4]), intersection=text(words[5]))
        out.append(sub)
    return out


def _library_exports(library):
    """The shader functions of a DXIL library by their names (dx.entryPoints)."""
    out = {}
    for name, (function, signatures, properties) in library.entries.items():
        if function is not None and name:
            # a mangled name ("\x01?RayGen@@YAXXZ") is exported under its unqualified name too, as in D3D12
            mangled = name.lstrip("\x01")
            out[mangled] = function
            if mangled.startswith("?") and "@" in mangled:
                out.setdefault(mangled[1:].split("@", 1)[0], function)
            else:
                out.setdefault(name, function)
    return out


# ------------------------------------------------------------------ the descriptions (read when recorded)
def read_inputs(dp, address) -> dict:
    """D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_INPUTS: the instances' address or the geometries."""
    kind, flags, count, layout = struct.unpack("<4I", bytes(dp.read(address, 16)))
    out = dict(type=kind, flags=flags, count=count, geometries=[], instances=0)
    if kind == 0:       # top level: the GPU address of the instance descs
        out["instances"] = struct.unpack("<Q", bytes(dp.read(address + 16, 8)))[0]
        return out
    ptr = dp.ptr_size()
    table = dp.read_ptr(address + 16)
    for k in range(count):
        at = dp.read_ptr(table + ptr * k) if layout == 1 else table + 56 * k
        g_type, g_flags = struct.unpack("<2I", bytes(dp.read(at, 8)))
        raw = bytes(dp.read(at + 8, 48))
        if g_type == 1:     # AABBs: count, start, stride
            aabb_count, start, stride = struct.unpack_from("<3Q", raw, 0)
            out["geometries"].append(dict(type=1, flags=g_flags, aabb_count=aabb_count, aabbs=start, aabb_stride=stride))
            continue
        transform, index_format, vertex_format, index_count, vertex_count, indices, vertices, stride = \
            struct.unpack("<Q4I3Q", raw)
        out["geometries"].append(dict(type=g_type, flags=g_flags, transform=transform, index_format=index_format,
                                      vertex_format=vertex_format, index_count=index_count, vertex_count=vertex_count,
                                      indices=indices, vertices=vertices, vertex_stride=stride))
    return out


def read_build(dp, address) -> dict:
    """D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC."""
    dest = struct.unpack("<Q", bytes(dp.read(address, 8)))[0]
    desc = read_inputs(dp, address + 8)
    desc["dest"] = dest
    desc["source"] = struct.unpack("<Q", bytes(dp.read(address + 32, 8)))[0]
    return desc


def prebuild_sizes(desc: dict):
    """Sizes for GetRaytracingAccelerationStructurePrebuildInfo (the structure lives in Python: small)."""
    if desc["type"] == 0:
        result = 256 + 64 * desc["count"]
    else:
        result = 256 + sum(64 * max(g.get("index_count") or g.get("vertex_count") or g.get("aabb_count") or 1, 1) // 3 + 64
                           for g in desc["geometries"])
    align = lambda v: (v + 255) & ~255      # noqa: E731
    return align(result), 256, 256


def read_dispatch(dp, address) -> dict:
    """D3D12_DISPATCH_RAYS_DESC."""
    raw = bytes(dp.read(address, 100))
    raygen = struct.unpack_from("<2Q", raw, 0)
    miss = struct.unpack_from("<3Q", raw, 16)
    hitgroups = struct.unpack_from("<3Q", raw, 40)
    callables = struct.unpack_from("<3Q", raw, 64)
    width, height, depth = struct.unpack_from("<3I", raw, 88)
    return dict(raygen=raygen, miss=miss, hitgroups=hitgroups, callables=callables, width=width, height=height, depth=depth)


# ------------------------------------------------------------------ building
def build(d3d, desc: dict) -> Optional[Structure]:
    """BuildRaytracingAccelerationStructure (at execution: the vertices are read then)."""
    if desc["type"] == 1:       # bottom level
        structure = Structure("bottom")
        for g in desc["geometries"]:
            if g["type"] == 1:
                structure.geometries.append(dict(kind="aabbs", flags=g["flags"], boxes=_read_aabbs(d3d, g)))
                continue
            vertices = _read_vertices(d3d, g)
            indices = _read_indices(d3d, g, len(vertices))
            triangles = vertices[indices].reshape(-1, 3, 3) if len(indices) else numpy.zeros((0, 3, 3))
            if g["transform"]:
                found = d3d.at_address(g["transform"])
                if found is not None:
                    resource, offset = found
                    m = numpy.frombuffer(resource.read()[offset:offset + 48].tobytes(), dtype="<f4").reshape(3, 4).astype(numpy.float64)
                    triangles = triangles @ m[:, :3].T + m[:, 3]
            structure.geometries.append(dict(kind="triangles", flags=g["flags"], triangles=triangles))
        return structure
    structure = Structure("top")
    found = d3d.at_address(desc["instances"]) if desc["count"] else None
    if found is not None:
        resource, offset = found
        data = resource.read()[offset:offset + 64 * desc["count"]].tobytes()
        for k in range(desc["count"]):
            raw = data[64 * k:64 * (k + 1)]
            if len(raw) < 64:
                break
            transform = numpy.frombuffer(raw[:48], dtype="<f4").reshape(3, 4).astype(numpy.float64)
            word1, word2, blas = struct.unpack_from("<IIQ", raw, 48)
            structure.instances.append(dict(transform=transform, id=word1 & 0xFFFFFF, mask=word1 >> 24,
                                            contribution=word2 & 0xFFFFFF, flags=word2 >> 24, blas=blas))
    return structure


VERTEX_FORMATS = {2: ("<f4", 3), 6: ("<f4", 3), 16: ("<f4", 2), 10: ("<f2", 4), 34: ("<f2", 2)}


def _read_vertices(d3d, g):
    found = d3d.at_address(g["vertices"])
    if found is None or not g["vertex_count"]:
        return numpy.zeros((0, 3))
    resource, offset = found
    kind, components = VERTEX_FORMATS.get(g["vertex_format"], ("<f4", 3))
    item = numpy.dtype(kind).itemsize
    stride = g["vertex_stride"] or item * components
    data = resource.read()
    out = numpy.zeros((g["vertex_count"], 3))
    for k in range(g["vertex_count"]):
        start = offset + k * stride
        values = numpy.frombuffer(data[start:start + item * components].tobytes(), dtype=kind).astype(numpy.float64)
        out[k, :min(3, len(values))] = values[:3]
    return out


def _read_indices(d3d, g, count):
    if not g["index_count"] or not g["indices"]:
        return numpy.arange(count - count % 3)
    found = d3d.at_address(g["indices"])
    if found is None:
        return numpy.zeros(0, dtype=numpy.int64)
    resource, offset = found
    kind = "<u4" if g["index_format"] == 42 else "<u2"
    size = 4 if kind == "<u4" else 2
    data = resource.read()[offset:offset + size * g["index_count"]].tobytes()
    return numpy.frombuffer(data, dtype=kind).astype(numpy.int64)


def _read_aabbs(d3d, g):
    """The boxes (k, 2, 3) of an AABB geometry (D3D12_RAYTRACING_AABB: min x y z, max x y z)."""
    found = d3d.at_address(g["aabbs"])
    count = int(g["aabb_count"])
    if found is None or not count:
        return numpy.zeros((0, 2, 3))
    resource, offset = found
    stride = int(g["aabb_stride"]) or 24
    data = resource.read()
    out = numpy.zeros((count, 2, 3))
    for k in range(count):
        start = offset + k * stride
        out[k] = numpy.frombuffer(data[start:start + 24].tobytes(), dtype="<f4").astype(numpy.float64).reshape(2, 3)
    return out


# ------------------------------------------------------------------ intersections
def _intersect(origin, direction, triangles):
    """Moller-Trumbore of n rays against k triangles: t, u, v (n, k) (inf where missed) and the facing."""
    a, b, c = triangles[:, 0], triangles[:, 1], triangles[:, 2]
    e1, e2 = b - a, c - a                                           # (k, 3)
    p = numpy.cross(direction[:, None, :], e2[None, :, :])          # (n, k, 3)
    det = (e1[None] * p).sum(axis=2)
    with numpy.errstate(divide="ignore", invalid="ignore"):
        inv = 1.0 / det
        s = origin[:, None, :] - a[None]
        u = (s * p).sum(axis=2) * inv
        q = numpy.cross(s, e1[None])
        v = (direction[:, None, :] * q).sum(axis=2) * inv
        t = (e2[None] * q).sum(axis=2) * inv
    ok = (numpy.abs(det) > 1e-12) & (u >= 0) & (v >= 0) & (u + v <= 1)
    t = numpy.where(ok, t, numpy.inf)
    # front: the vertices clockwise seen from the ray's origin (left-handed: det > 0 with this order)
    return t, u, v, det > 0


def _slabs(origin, direction, boxes, tmin, tmax):
    """The entry t (n, k) of the rays into the boxes (inf when they miss them within [tmin, tmax])."""
    lo, hi = boxes[:, 0], boxes[:, 1]
    with numpy.errstate(divide="ignore", invalid="ignore"):
        inv = 1.0 / direction[:, None, :]
        t0 = (lo[None] - origin[:, None, :]) * inv
        t1 = (hi[None] - origin[:, None, :]) * inv
    near = numpy.nan_to_num(numpy.minimum(t0, t1), nan=-numpy.inf).max(axis=2)
    far = numpy.nan_to_num(numpy.maximum(t0, t1), nan=numpy.inf).min(axis=2)
    entry = numpy.maximum(near, tmin[:, None])
    ok = (far >= entry) & (entry <= tmax[:, None])
    return numpy.where(ok, entry, numpy.inf)


def candidates(structures, structure, origin, direction, tmin, tmax, include, flags, lanes):
    """The candidates of the rays of the lanes: [dict kind ("triangle" / "aabb"), t (n, k), instance index,
    geometry index, opaque, and u, v, front for triangles]."""
    out = []
    if structure is None:
        return out
    for i, instance in enumerate(structure.instances):
        visible = lanes & ((instance["mask"] & include) != 0)
        if not visible.any():
            continue
        blas = structures.get(instance["blas"])
        if blas is None:
            continue
        full = numpy.vstack([instance["transform"], [0, 0, 0, 1]])
        try:
            inverse = numpy.linalg.inv(full)[:3]
        except numpy.linalg.LinAlgError:
            continue
        o = origin @ inverse[:, :3].T + inverse[:, 3]
        d = direction @ inverse[:, :3].T
        for g, geometry in enumerate(blas.geometries):
            opaque = bool(geometry["flags"] & GEOMETRY_OPAQUE)
            if instance["flags"] & INSTANCE_FORCE_OPAQUE:
                opaque = True
            if instance["flags"] & INSTANCE_FORCE_NON_OPAQUE:
                opaque = False
            ray_flags = numpy.broadcast_to(numpy.asarray(flags, dtype=numpy.int64), lanes.shape)
            forced = numpy.where(ray_flags & RAY_FLAG_FORCE_OPAQUE, True, numpy.where(ray_flags & RAY_FLAG_FORCE_NON_OPAQUE,
                                                                                     False, opaque))
            keep = visible & ~(forced & ((ray_flags & RAY_FLAG_CULL_OPAQUE) != 0)) & \
                ~(~forced & ((ray_flags & RAY_FLAG_CULL_NON_OPAQUE) != 0))
            if geometry["kind"] == "triangles":
                tris = geometry["triangles"]
                if not len(tris):
                    continue
                keep = keep & ((ray_flags & RAY_FLAG_SKIP_TRIANGLES) == 0)
                t, u, v, front = _intersect(o, d, tris)
                if instance["flags"] & INSTANCE_FRONT_CCW:
                    front = ~front
                ok = numpy.isfinite(t) & (t >= tmin[:, None]) & (t <= tmax[:, None]) & keep[:, None]
                if not instance["flags"] & INSTANCE_CULL_DISABLE:
                    ok &= ~(((ray_flags & RAY_FLAG_CULL_BACK) != 0)[:, None] & ~front)
                    ok &= ~(((ray_flags & RAY_FLAG_CULL_FRONT) != 0)[:, None] & front)
                out.append(dict(kind="triangle", t=numpy.where(ok, t, numpy.inf), instance=i, geometry=g, opaque=forced,
                                u=u, v=v, front=front))
            else:
                boxes = geometry["boxes"]
                if not len(boxes):
                    continue
                keep = keep & ((ray_flags & RAY_FLAG_SKIP_PROCEDURAL) == 0)
                entry = _slabs(o, d, boxes, tmin, tmax)
                out.append(dict(kind="aabb", t=numpy.where(keep[:, None], entry, numpy.inf), instance=i, geometry=g,
                                opaque=forced))
    return out


def _object_space(instance, origin, direction):
    full = numpy.vstack([instance["transform"], [0, 0, 0, 1]])
    inverse = numpy.linalg.inv(full)[:3]
    return inverse, origin @ inverse[:, :3].T + inverse[:, 3], direction @ inverse[:, :3].T


# ------------------------------------------------------------------ dispatching
class Rays:
    """The run of a DispatchRays: the state object, the shader tables, the bindings, the structures.
    local_bindings(root, record, base): the bindings of a shader record's local root arguments."""

    def __init__(self, d3d, state: StateObject, desc: dict, bindings, local_bindings=None):
        self.d3d, self.state, self.desc, self.bindings = d3d, state, desc, bindings
        self.local_bindings = local_bindings
        self.cache: Dict[bytes, object] = {}

    def record(self, table, index):
        """(export name, export, record bytes) of a record of a shader table, or None."""
        start, size, *rest = table
        stride = rest[0] if rest else 0
        if not start:
            return None
        found = self.d3d.at_address(start + index * stride)
        if found is None:
            return None
        resource, offset = found
        length = max(32, int(stride or size or 32))
        data = resource.read()[offset:offset + length].tobytes()
        name = self.state.lookup(data)
        if name is None:
            return None
        return name, self.state.exports.get(name), data

    def bindings_of(self, name, data):
        root = self.state.local_root(name)
        if root is None or self.local_bindings is None:
            return self.bindings
        key = (name, data)
        if key not in self.cache:
            self.cache[key] = self.local_bindings(root, data, self.bindings)
        return self.cache[key]

    def run(self):
        desc = self.desc
        width, height, depth = desc["width"], desc["height"], desc["depth"]
        n = width * height * depth
        if not n:
            return
        found = self.record((desc["raygen"][0], desc["raygen"][1], 0), 0)
        if found is None or found[1] is None or found[1][0] != "shader":
            return
        name, (_, library, function), data = found
        lane = numpy.arange(n)
        index = numpy.stack([lane % width, (lane // width) % height, lane // (width * height)], 1)
        system = {"rays_index": index, "rays_dims": numpy.tile([width, height, depth], (n, 1))}
        self.invoke(library, function, n, system, numpy.ones(n, dtype=bool), [], self.bindings_of(name, data))

    def invoke(self, library, function, n, system, mask, args, bindings=None, context=None):
        machine = Machine(library, function, n, bindings if bindings is not None else self.bindings, system=system)
        machine.callbacks = CALLBACKS
        machine.rays = self
        machine.traversal = context
        machine.run(mask, args)
        return machine

    def structure(self, m, handle):
        view = m._view(handle) if isinstance(handle, Handle) else None
        address = getattr(view, "address", None) if view is not None else None
        return self.d3d.structures.get(address) if address is not None else None

    # TraceRay ------------------------------------------------------------------------------------
    def trace(self, m: Machine, args, lanes):
        Traversal(self, m, args, lanes).run()
        return None


class Traversal:
    """A TraceRay of the active lanes of a shader: the candidates nearest first, the committed hit, then the
    closest hit or miss shaders."""

    def __init__(self, rays: Rays, m: Machine, args, lanes):
        self.rays, self.m, self.n = rays, m, m.n
        n = self.n
        self.structure = rays.structure(m, args[0])
        self.flags = numpy.broadcast_to(numpy.asarray(args[1], dtype=numpy.int64), (n,)).copy()
        self.include = numpy.broadcast_to(numpy.asarray(args[2], dtype=numpy.int64), (n,))
        self.contribution = numpy.broadcast_to(numpy.asarray(args[3], dtype=numpy.int64), (n,))
        self.multiplier = numpy.broadcast_to(numpy.asarray(args[4], dtype=numpy.int64), (n,))
        self.miss_index = numpy.broadcast_to(numpy.asarray(args[5], dtype=numpy.int64), (n,))
        self.origin = numpy.stack([numpy.asarray(a, dtype=numpy.float64) for a in args[6:9]], 1)
        self.tmin = numpy.broadcast_to(numpy.asarray(args[9], dtype=numpy.float64), (n,)).copy()
        self.direction = numpy.stack([numpy.asarray(a, dtype=numpy.float64) for a in args[10:13]], 1)
        self.tmax = numpy.broadcast_to(numpy.asarray(args[13], dtype=numpy.float64), (n,)).copy()
        self.payload = args[14]
        self.lanes = lanes.copy()
        # the committed hit
        self.best = self.tmax.copy()                 # RayTCurrent
        self.hit = numpy.zeros(n, dtype=bool)
        self.instance = numpy.full(n, -1)
        self.geometry = numpy.zeros(n, dtype=numpy.int64)
        self.primitive = numpy.zeros(n, dtype=numpy.int64)
        self.kind = numpy.zeros(n, dtype=numpy.int64)
        self.u, self.v = numpy.zeros(n), numpy.zeros(n)
        self.procedural = numpy.zeros(n, dtype=bool)
        self.attributes = None                       # procedural: (types, cells per slot (n,))
        self.ended = numpy.zeros(n, dtype=bool)

    def hitgroup(self, instance, geometry, lanes):
        """[(name, hit group, record data, lanes)] of the lanes: record contribution + multiplier * geometry +
        the instance's."""
        out = []
        index = self.contribution + self.multiplier * geometry + instance["contribution"]
        for value in numpy.unique(index[lanes]):
            found = self.rays.record(self.rays.desc["hitgroups"], int(value))
            if found is not None and found[1] is not None and found[1][0] == "hitgroup":
                out.append((found[0], found[1][1], found[2], lanes & (index == value)))
            else:
                out.append((None, None, None, lanes & (index == value)))
        return out

    def system(self, instance_index, geometry, primitive, kind, t):
        n = self.n
        instance = self.structure.instances[instance_index]
        inverse, o, d = _object_space(instance, self.origin, self.direction)
        system = dict(self.m.system)
        system.update(world_origin=self.origin, world_direction=self.direction, tmin=self.tmin, tcurrent=t,
                      object_origin=o, object_direction=d,
                      object_to_world=numpy.broadcast_to(instance["transform"], (n, 3, 4)),
                      world_to_object=numpy.broadcast_to(inverse, (n, 3, 4)),
                      instance_id=numpy.full(n, instance["id"]), instance_index=numpy.full(n, instance_index),
                      primitive_index=numpy.broadcast_to(numpy.asarray(primitive), (n,)), geometry_index=numpy.full(n, geometry),
                      hit_kind=numpy.broadcast_to(numpy.asarray(kind), (n,)), ray_flags=self.flags)
        return system

    def commit(self, lanes, t, instance, geometry, primitive, kind, u=None, v=None, attributes=None):
        self.hit |= lanes
        self.best = numpy.where(lanes, t, self.best)
        self.instance = numpy.where(lanes, instance, self.instance)
        self.geometry = numpy.where(lanes, geometry, self.geometry)
        self.primitive = numpy.where(lanes, primitive, self.primitive)
        self.kind = numpy.where(lanes, kind, self.kind)
        self.procedural = numpy.where(lanes, attributes is not None, self.procedural)
        if u is not None:
            self.u = numpy.where(lanes, u, self.u)
            self.v = numpy.where(lanes, v, self.v)
        if attributes is not None:
            types, cells = attributes
            if self.attributes is None or len(self.attributes[1]) != len(cells):
                self.attributes = (types, [numpy.zeros(self.n, dtype=c.dtype) for c in cells])
            for target, cell in zip(self.attributes[1], cells):
                target[lanes] = cell[lanes]
        first = lanes & ((self.flags & RAY_FLAG_ACCEPT_FIRST_HIT) != 0)
        self.ended |= first

    def any_hit(self, name, group, data, lanes, system, attributes_pointer):
        """The any hit shader of a hit group on the lanes: (accepted, end the search)."""
        accepted, end = lanes.copy(), numpy.zeros(self.n, dtype=bool)
        if group is None or not group.get("anyhit"):
            return accepted, end
        export = self.rays.state.exports.get(group["anyhit"])
        if export is None or export[0] != "shader":
            return accepted, end
        machine = self.rays.invoke(export[1], export[2], self.n, system, lanes, [self.payload, attributes_pointer],
                                   self.rays.bindings_of(name, data))
        accepted &= ~getattr(machine, "ignored", numpy.zeros(self.n, dtype=bool))
        end |= lanes & getattr(machine, "accepted_end", numpy.zeros(self.n, dtype=bool))
        return accepted, end

    def run(self):
        self.traverse()
        self._shade()

    def traverse(self):
        """The candidates nearest first, intersection and any hit shaders included: the committed hit."""
        n = self.n
        found = candidates(self.rays.d3d.structures, self.structure, self.origin, self.direction, self.tmin, self.tmax,
                           self.include, self.flags, self.lanes)
        considered = [numpy.zeros_like(c["t"], dtype=bool) for c in found]
        searching = self.lanes.copy()
        rows = numpy.arange(n)
        while found and searching.any():
            best = numpy.full(n, numpy.inf)
            which = numpy.full(n, -1)
            column = numpy.zeros(n, dtype=numpy.int64)
            for k, c in enumerate(found):
                masked = numpy.where(considered[k], numpy.inf, c["t"])
                j = masked.argmin(axis=1)
                tj = masked[rows, j]
                better = (tj < best) & (tj <= self.best) & searching
                best = numpy.where(better, tj, best)
                which = numpy.where(better, k, which)
                column = numpy.where(better, j, column)
            have = searching & (which >= 0)
            if not have.any():
                break
            for k, c in enumerate(found):
                sel = have & (which == k)
                if not sel.any():
                    continue
                considered[k][numpy.nonzero(sel)[0], column[sel]] = True
                instance = self.structure.instances[c["instance"]]
                if c["kind"] == "triangle":
                    self._triangle(c, sel, column, best, instance)
                else:
                    self._aabb(c, sel, column, instance)
            searching &= ~self.ended

    def _triangle(self, c, sel, column, t, instance):
        n = self.n
        rows = numpy.arange(n)
        u, v, front = c["u"][rows, column], c["v"][rows, column], c["front"][rows, column]
        kind = numpy.where(front, HIT_KIND_FRONT, HIT_KIND_BACK)
        opaque = numpy.broadcast_to(c["opaque"], (n,)) & sel
        if opaque.any():
            self.commit(opaque, t, c["instance"], c["geometry"], column, kind, u, v)
        rest = sel & ~opaque
        if not rest.any():
            return
        for name, group, data, lanes in self.hitgroup(instance, c["geometry"], rest):
            system = self.system(c["instance"], c["geometry"], column, kind, t)
            accepted, end = self.any_hit(name, group, data, lanes, system, _attributes(n, u, v))
            if accepted.any():
                self.commit(accepted, t, c["instance"], c["geometry"], column, kind, u, v)
            self.ended |= end

    def _aabb(self, c, sel, column, instance):
        """The intersection shader of the AABB's hit group; its ReportHit commits (callback below)."""
        for name, group, data, lanes in self.hitgroup(instance, c["geometry"], sel):
            if group is None or not group.get("intersection"):
                continue
            export = self.rays.state.exports.get(group["intersection"])
            if export is None or export[0] != "shader":
                continue
            system = self.system(c["instance"], c["geometry"], column, 0, self.best)
            self.reporting = dict(name=name, group=group, data=data, instance=c["instance"], geometry=c["geometry"],
                                  primitive=column.copy(), opaque=numpy.broadcast_to(c["opaque"], (self.n,)))
            self.rays.invoke(export[1], export[2], self.n, system, lanes, [], self.rays.bindings_of(name, data), self)

    def report(self, m, t, kind, pointer, active):
        """ReportHit from an intersection shader: the hits within [RayTMin, RayTCurrent] go to the any hit shader
        (non opaque geometry), then commit; True for the lanes that committed."""
        r = self.reporting
        t = numpy.asarray(t, dtype=numpy.float64)
        ok = active & (t >= self.tmin) & (t <= self.best) & ~self.ended
        out = numpy.zeros(self.n, dtype=numpy.int64)
        if not ok.any():
            return out
        cells, types = [], []
        if isinstance(pointer, Pointer):     # the attributes: the scalars of the memory the pointer starts
            memory = pointer.memory
            rows = numpy.broadcast_to(pointer.rows, (self.n,))
            first = int(numpy.asarray(pointer.offset).ravel()[0])
            for k in range(first, len(memory.cells)):
                cells.append(memory.cells[k][rows].copy())
                types.append(memory.types[k])
        system = self.system(r["instance"], r["geometry"], r["primitive"], kind, t)
        accepted = ok.copy()
        end = numpy.zeros(self.n, dtype=bool)
        non_opaque = ok & ~r["opaque"]
        if non_opaque.any():
            copy = _copy_attributes(self.n, types, cells)
            got, end = self.any_hit(r["name"], r["group"], r["data"], non_opaque, system, copy)
            accepted = (ok & r["opaque"]) | got
        if accepted.any():
            self.commit(accepted, t, r["instance"], r["geometry"], r["primitive"], numpy.asarray(kind, dtype=numpy.int64),
                        attributes=(types, cells))
            m.system["tcurrent"] = self.best
        self.ended |= end
        stop = (end | (accepted & ((self.flags & RAY_FLAG_ACCEPT_FIRST_HIT) != 0)))
        if stop.any():
            m.discarded |= stop        # the intersection shader ends for those lanes
        out[accepted] = 1
        return out

    def _shade(self):
        n = self.n
        hit, lanes = self.hit & self.lanes, self.lanes
        skip = (self.flags & RAY_FLAG_SKIP_CLOSEST_HIT) != 0
        shade = hit & ~skip
        if shade.any():
            for i in numpy.unique(self.instance[shade]):
                instance = self.structure.instances[int(i)]
                for g in numpy.unique(self.geometry[shade & (self.instance == i)]):
                    sel = shade & (self.instance == i) & (self.geometry == g)
                    for name, group, data, lanes_group in self.hitgroup(instance, int(g), sel):
                        if group is None or not group.get("closest"):
                            continue
                        export = self.rays.state.exports.get(group["closest"])
                        if export is None or export[0] != "shader":
                            continue
                        system = self.system(int(i), int(g), self.primitive, self.kind, self.best)
                        if self.procedural[lanes_group].any() and self.attributes is not None:
                            attributes = _copy_attributes(n, *self.attributes)
                        else:
                            attributes = _attributes(n, self.u, self.v)
                        self.rays.invoke(export[1], export[2], n, system, lanes_group, [self.payload, attributes],
                                         self.rays.bindings_of(name, data))
        missed = lanes & ~hit
        if missed.any():
            for index in numpy.unique(self.miss_index[missed]):
                sel = missed & (self.miss_index == index)
                found = self.rays.record(self.rays.desc["miss"], int(index))
                if found is None or found[1] is None or found[1][0] != "shader":
                    continue
                system = dict(self.m.system)
                system.update(world_origin=self.origin, world_direction=self.direction, tmin=self.tmin, tcurrent=self.tmax,
                              ray_flags=self.flags)
                self.rays.invoke(found[1][1], found[1][2], n, system, sel, [self.payload], self.rays.bindings_of(found[0], found[2]))


def _attributes(n, u, v):
    memory = Memory(Type("struct", elements=[Type("float", 32), Type("float", 32)]), n)
    memory.cells[0][:] = u
    memory.cells[1][:] = v
    return Pointer(memory, numpy.zeros(n, dtype=numpy.int64), numpy.arange(n))


def _copy_attributes(n, types, cells):
    """User attributes in a memory of their own (for an any hit or closest hit shader)."""
    memory = Memory(Type("struct", elements=list(types)), n) if types else Memory(Type("struct", elements=[Type("float", 32)]), n)
    for target, cell in zip(memory.cells, cells):
        target[:] = cell.astype(target.dtype)
    return Pointer(memory, numpy.zeros(n, dtype=numpy.int64), numpy.arange(n))


# ------------------------------------------------------------------ RayQuery
class Query:
    """The state of a RayQuery on every lane."""

    def __init__(self, n, flags):
        self.n, self.const_flags = n, flags
        self.found = []
        self.order = None
        self.position = numpy.zeros(n, dtype=numpy.int64)
        self.flags = numpy.full(n, flags, dtype=numpy.int64)
        self.origin = numpy.zeros((n, 3))
        self.direction = numpy.zeros((n, 3))
        self.tmin = numpy.zeros(n)
        self.tmax = numpy.zeros(n)
        self.structure = None
        self.done = numpy.ones(n, dtype=bool)
        self.status = numpy.zeros(n, dtype=numpy.int64)       # 0 nothing, 1 triangle, 2 procedural
        self.committed = {}
        self.candidate = {}
        for table in (self.committed, self.candidate):
            table.update(t=numpy.zeros(n), instance=numpy.zeros(n, dtype=numpy.int64), geometry=numpy.zeros(n, dtype=numpy.int64),
                         primitive=numpy.zeros(n, dtype=numpy.int64), u=numpy.zeros(n), v=numpy.zeros(n),
                         front=numpy.zeros(n, dtype=bool), kind=numpy.zeros(n, dtype=numpy.int64),
                         opaque=numpy.zeros(n, dtype=bool))

    def trace(self, structures, structure, flags, include, origin, tmin, direction, tmax, lanes):
        n = self.n
        self.structure = structure
        self.flags = numpy.where(lanes, flags | self.const_flags, self.flags)
        self.origin = numpy.where(lanes[:, None], origin, self.origin)
        self.direction = numpy.where(lanes[:, None], direction, self.direction)
        self.tmin = numpy.where(lanes, tmin, self.tmin)
        self.tmax = numpy.where(lanes, tmax, self.tmax)
        self.committed["t"] = numpy.where(lanes, tmax, self.committed["t"])
        self.status = numpy.where(lanes, 0, self.status)
        self.found = candidates(structures, structure, self.origin, self.direction, self.tmin, self.tmax, include,
                                self.flags, lanes)
        # every candidate of each lane, nearest first: (group, column) by position
        if self.found:
            t = numpy.concatenate([c["t"] for c in self.found], axis=1)
            group = numpy.concatenate([numpy.full(c["t"].shape[1], k) for k, c in enumerate(self.found)])
            column = numpy.concatenate([numpy.arange(c["t"].shape[1]) for c in self.found])
            order = numpy.argsort(t, axis=1, kind="stable")
            self.sorted_t = numpy.take_along_axis(t, order, axis=1)
            self.sorted_group, self.sorted_column = group[order], column[order]
        else:
            self.sorted_t = numpy.full((n, 0), numpy.inf)
            self.sorted_group = self.sorted_column = numpy.zeros((n, 0), dtype=numpy.int64)
        self.position = numpy.where(lanes, 0, self.position)
        self.done = numpy.where(lanes, False, self.done)

    def _take(self, table, lanes, place):
        rows = numpy.nonzero(lanes)[0]
        for row in rows:
            k, j = int(self.sorted_group[row, place[row]]), int(self.sorted_column[row, place[row]])
            c = self.found[k]
            instance = self.structure.instances[c["instance"]]
            table["t"][row] = self.sorted_t[row, place[row]]
            table["instance"][row] = c["instance"]
            table["geometry"][row] = c["geometry"]
            table["primitive"][row] = j
            table["opaque"][row] = bool(numpy.broadcast_to(c["opaque"], (self.n,))[row])
            table["kind"][row] = 0 if c["kind"] == "triangle" else 1
            if c["kind"] == "triangle":
                table["u"][row], table["v"][row] = c["u"][row, j], c["v"][row, j]
                table["front"][row] = c["front"][row, j]
            del instance

    def proceed(self, lanes):
        """Proceed: the opaque triangles committed on the way; True where a candidate waits for the shader."""
        n = self.n
        out = numpy.zeros(n, dtype=bool)
        walking = lanes & ~self.done
        count = self.sorted_t.shape[1]
        while walking.any():
            place = self.position
            exhausted = walking & (place >= count)
            self.done |= exhausted
            walking &= ~exhausted
            if not walking.any():
                break
            t = self.sorted_t[numpy.arange(n), numpy.minimum(place, max(count - 1, 0))]
            beyond = walking & ~(t <= self.committed["t"])
            self.done |= beyond
            walking &= ~beyond
            if not walking.any():
                break
            self._take(self.candidate, walking, place)
            self.position = numpy.where(walking, place + 1, self.position)
            opaque_triangle = walking & self.candidate["opaque"] & (self.candidate["kind"] == 0)
            if opaque_triangle.any():
                self._commit(opaque_triangle, self.candidate["t"], 1)
            stop = walking & ~opaque_triangle
            out |= stop
            walking &= ~stop
            walking &= ~self.done
        return out

    def _commit(self, lanes, t, status):
        for key in self.committed:
            self.committed[key] = numpy.where(lanes, self.candidate[key], self.committed[key])
        self.committed["t"] = numpy.where(lanes, t, self.committed["t"])
        self.status = numpy.where(lanes, status, self.status)
        first = lanes & ((self.flags & RAY_FLAG_ACCEPT_FIRST_HIT) != 0)
        self.done |= first


def _queries(m):
    return m.__dict__.setdefault("ray_queries", [])


def _query(m, handle):
    ids = numpy.asarray(handle, dtype=numpy.int64)
    queries = _queries(m)
    index = int(ids.ravel()[0]) if ids.size else 0
    return queries[index] if 0 <= index < len(queries) else None


def _allocate_query(m, args, ins, lanes, active):
    queries = _queries(m)
    flags = int(numpy.asarray(args[0]).ravel()[0]) if args else 0
    queries.append(Query(m.n, flags))
    return numpy.full(m.n, len(queries) - 1, dtype=numpy.int64)


def _trace_inline(m, args, ins, lanes, active):
    q = _query(m, args[0])
    if q is None:
        return None
    view = m._view(args[1]) if isinstance(args[1], Handle) else None
    structures = getattr(view, "structures", None) or {}
    structure = structures.get(getattr(view, "address", None))
    n = m.n
    flags = numpy.broadcast_to(numpy.asarray(args[2], dtype=numpy.int64), (n,))
    include = numpy.broadcast_to(numpy.asarray(args[3], dtype=numpy.int64), (n,))
    origin = numpy.stack([numpy.broadcast_to(numpy.asarray(a, dtype=numpy.float64), (n,)) for a in args[4:7]], 1)
    tmin = numpy.broadcast_to(numpy.asarray(args[7], dtype=numpy.float64), (n,))
    direction = numpy.stack([numpy.broadcast_to(numpy.asarray(a, dtype=numpy.float64), (n,)) for a in args[8:11]], 1)
    tmax = numpy.broadcast_to(numpy.asarray(args[11], dtype=numpy.float64), (n,))
    q.trace(structures, structure, flags, include, origin, tmin, direction, tmax, active)
    return None


def _proceed(m, args, ins, lanes, active):
    q = _query(m, args[0])
    return q.proceed(active).astype(numpy.int64) if q is not None else numpy.zeros(m.n, dtype=numpy.int64)


def _abort(m, args, ins, lanes, active):
    q = _query(m, args[0])
    if q is not None:
        q.done |= active
    return None


def _commit_triangle(m, args, ins, lanes, active):
    q = _query(m, args[0])
    if q is not None:
        q._commit(active & (q.candidate["kind"] == 0), q.candidate["t"], 1)
    return None


def _commit_procedural(m, args, ins, lanes, active):
    q = _query(m, args[0])
    if q is not None:
        q._commit(active & (q.candidate["kind"] == 1), numpy.asarray(args[1], dtype=numpy.float64), 2)
    return None


def _query_value(table, key, kind=None):
    def run(m, args, ins, lanes, active):
        q = _query(m, args[0])
        if q is None:
            return numpy.zeros(m.n, dtype=numpy.float32 if ins.type.kind == "float" else numpy.int64)
        values = (q.committed if table == "committed" else q.candidate)[key]
        if key == "instance_id" or key == "contribution":
            pass
        if ins.type.kind == "float":
            return values.astype(numpy.float32)
        return values.astype(numpy.int64)
    return run


def _query_instance(table, field):
    """InstanceID / InstanceContributionToHitGroupIndex of the committed or candidate instance."""
    def run(m, args, ins, lanes, active):
        q = _query(m, args[0])
        out = numpy.zeros(m.n, dtype=numpy.int64)
        if q is None or q.structure is None:
            return out
        index = (q.committed if table == "committed" else q.candidate)["instance"]
        values = numpy.array([inst[field] for inst in q.structure.instances] or [0])
        return values[numpy.clip(index, 0, len(values) - 1)].astype(numpy.int64)
    return run


def _query_matrix(table, inverse):
    def run(m, args, ins, lanes, active):
        q = _query(m, args[0])
        row, column = int(numpy.asarray(args[1]).ravel()[0]), int(numpy.asarray(args[2]).ravel()[0])
        out = numpy.zeros(m.n, dtype=numpy.float32)
        if q is None or q.structure is None:
            return out
        index = (q.committed if table == "committed" else q.candidate)["instance"]
        for i in numpy.unique(index):
            instance = q.structure.instances[int(i)]
            matrix = numpy.linalg.inv(numpy.vstack([instance["transform"], [0, 0, 0, 1]]))[:3] if inverse else instance["transform"]
            out[index == i] = matrix[row, column]
        return out
    return run


def _query_object_ray(table, direction):
    def run(m, args, ins, lanes, active):
        q = _query(m, args[0])
        c = int(numpy.asarray(args[1]).ravel()[0])
        out = numpy.zeros(m.n, dtype=numpy.float32)
        if q is None or q.structure is None:
            return out
        index = (q.committed if table == "committed" else q.candidate)["instance"]
        for i in numpy.unique(index):
            instance = q.structure.instances[int(i)]
            _, o, d = _object_space(instance, q.origin, q.direction)
            sel = index == i
            out[sel] = (d if direction else o)[sel, c]
        return out
    return run


def _query_ray(key):
    def run(m, args, ins, lanes, active):
        q = _query(m, args[0])
        if q is None:
            return numpy.zeros(m.n, dtype=numpy.float32 if ins.type.kind == "float" else numpy.int64)
        value = getattr(q, key)
        if value.ndim == 2:
            value = value[:, int(numpy.asarray(args[1]).ravel()[0])]
        return value.astype(numpy.float32) if ins.type.kind == "float" else value.astype(numpy.int64)
    return run


def _query_barycentrics(table):
    def run(m, args, ins, lanes, active):
        q = _query(m, args[0])
        if q is None:
            return numpy.zeros(m.n, dtype=numpy.float32)
        values = q.committed if table == "committed" else q.candidate
        return (values["u"] if int(numpy.asarray(args[1]).ravel()[0]) == 0 else values["v"]).astype(numpy.float32)
    return run


def _query_status(m, args, ins, lanes, active):
    q = _query(m, args[0])
    return q.status.astype(numpy.int64) if q is not None else numpy.zeros(m.n, dtype=numpy.int64)


def _query_front(table):
    def run(m, args, ins, lanes, active):
        q = _query(m, args[0])
        if q is None:
            return numpy.zeros(m.n, dtype=numpy.int64)
        return (q.committed if table == "committed" else q.candidate)["front"].astype(numpy.int64)
    return run


def _query_candidate_non_opaque(m, args, ins, lanes, active):
    q = _query(m, args[0])
    return (~q.candidate["opaque"]).astype(numpy.int64) if q is not None else numpy.zeros(m.n, dtype=numpy.int64)



# ------------------------------------------------------------------ shader execution reordering (HitObject, 6.9)
class HitObject:
    """The result of a traversal without its shaders, per lane: nop (0), hit (1) or miss (2), the ray, the committed
    hit and the index of the shader record Invoke runs (hit group, or miss shader)."""
    FIELDS = ("state", "instance", "geometry", "primitive", "kind", "t", "tmin", "flags", "origin", "direction", "u", "v",
              "index", "procedural")

    def __init__(self, n, rays=None, structure=None):
        self.n, self.rays, self.structure = n, rays, structure
        self.state = numpy.zeros(n, dtype=numpy.int64)
        self.instance = numpy.zeros(n, dtype=numpy.int64)
        self.geometry = numpy.zeros(n, dtype=numpy.int64)
        self.primitive = numpy.zeros(n, dtype=numpy.int64)
        self.kind = numpy.zeros(n, dtype=numpy.int64)
        self.t = numpy.zeros(n)
        self.tmin = numpy.zeros(n)
        self.flags = numpy.zeros(n, dtype=numpy.int64)
        self.origin = numpy.zeros((n, 3))
        self.direction = numpy.zeros((n, 3))
        self.u, self.v = numpy.zeros(n), numpy.zeros(n)
        self.index = numpy.zeros(n, dtype=numpy.int64)
        self.procedural = numpy.zeros(n, dtype=bool)
        self.attributes = None

    def copy(self):
        out = HitObject(self.n, self.rays, self.structure)
        for name in self.FIELDS:
            setattr(out, name, getattr(self, name).copy())
        out.attributes = self.attributes
        return out

    def merge(self, mask, other):
        """The lanes of mask from self, the others from other (a phi or a select of hit objects)."""
        if not isinstance(other, HitObject):
            return self
        out = other.copy()
        out.rays = self.rays or other.rays
        out.structure = self.structure if self.structure is not None else other.structure
        for name in self.FIELDS:
            mine = getattr(self, name)
            where = mask[:, None] if mine.ndim == 2 else mask
            setattr(out, name, numpy.where(where, mine, getattr(other, name)))
        if self.attributes is not None:
            out.attributes = self.attributes
        return out

    def instance_of(self, lane):
        return self.structure.instances[int(self.instance[lane])] if self.structure is not None else None

    def object_space(self):
        """Per lane: the object to world and world to object matrices (n, 3, 4), the object ray."""
        n = self.n
        o2w = numpy.zeros((n, 3, 4))
        w2o = numpy.zeros((n, 3, 4))
        o2w[:, :, :3] = w2o[:, :, :3] = numpy.eye(3)
        if self.structure is not None:
            for i in numpy.unique(self.instance[self.state == 1]):
                instance = self.structure.instances[int(i)]
                sel = (self.instance == i) & (self.state == 1)
                o2w[sel] = instance["transform"]
                w2o[sel] = numpy.linalg.inv(numpy.vstack([instance["transform"], [0, 0, 0, 1]]))[:3]
        origin = numpy.einsum("nij,nj->ni", w2o[:, :, :3], self.origin) + w2o[:, :, 3]
        direction = numpy.einsum("nij,nj->ni", w2o[:, :, :3], self.direction)
        return o2w, w2o, origin, direction


def _hit_object_from_traversal(traversal):
    n = traversal.n
    h = HitObject(n, traversal.rays, traversal.structure)
    lanes = traversal.lanes
    hit = traversal.hit & lanes
    h.state = numpy.where(hit, 1, numpy.where(lanes, 2, 0))
    h.instance = numpy.where(hit, numpy.maximum(traversal.instance, 0), 0)
    h.geometry, h.primitive, h.kind = traversal.geometry.copy(), traversal.primitive.copy(), traversal.kind.copy()
    h.t = numpy.where(hit, traversal.best, traversal.tmax)
    h.tmin, h.flags = traversal.tmin.copy(), traversal.flags.copy()
    h.origin, h.direction = traversal.origin.copy(), traversal.direction.copy()
    h.u, h.v, h.procedural = traversal.u.copy(), traversal.v.copy(), traversal.procedural.copy()
    h.attributes = traversal.attributes
    contribution = numpy.zeros(n, dtype=numpy.int64)
    if traversal.structure is not None and traversal.structure.instances:
        table = numpy.array([inst["contribution"] for inst in traversal.structure.instances])
        contribution = table[numpy.clip(h.instance, 0, len(table) - 1)]
    h.index = numpy.where(hit, traversal.contribution + traversal.multiplier * h.geometry + contribution, traversal.miss_index)
    return h


def _hit_object_trace(m, args, ins, lanes, active):
    traversal = Traversal(m.rays, m, args, active)
    traversal.traverse()
    return _hit_object_from_traversal(traversal)


def _hit_object_miss(m, args, ins, lanes, active):
    n = m.n
    h = HitObject(n, getattr(m, "rays", None))
    h.state = numpy.where(active, 2, 0)
    h.flags = numpy.broadcast_to(numpy.asarray(args[0], dtype=numpy.int64), (n,)).copy()
    h.index = numpy.broadcast_to(numpy.asarray(args[1], dtype=numpy.int64), (n,)).copy()
    h.origin = numpy.stack([numpy.broadcast_to(numpy.asarray(a, dtype=numpy.float64), (n,)) for a in args[2:5]], 1)
    h.tmin = numpy.broadcast_to(numpy.asarray(args[5], dtype=numpy.float64), (n,)).copy()
    h.direction = numpy.stack([numpy.broadcast_to(numpy.asarray(a, dtype=numpy.float64), (n,)) for a in args[6:9]], 1)
    h.t = numpy.broadcast_to(numpy.asarray(args[9], dtype=numpy.float64), (n,)).copy()
    return h


def _hit_object_nop(m, args, ins, lanes, active):
    return HitObject(m.n, getattr(m, "rays", None))


def _hit_object_from_query(m, args, ins, lanes, active):
    q = _query(m, args[0])
    n = m.n
    h = HitObject(n, getattr(m, "rays", None), q.structure if q is not None else None)
    if q is None:
        return h
    c = q.committed
    hit = active & (q.status != 0)
    h.state = numpy.where(hit, 1, numpy.where(active, 2, 0))
    h.instance, h.geometry, h.primitive = c["instance"].copy(), c["geometry"].copy(), c["primitive"].copy()
    h.kind = numpy.where(q.status == 1, numpy.where(c["front"], HIT_KIND_FRONT, HIT_KIND_BACK), 0)
    if len(args) > 1:
        h.kind = numpy.where(hit, numpy.broadcast_to(numpy.asarray(args[1], dtype=numpy.int64), (n,)), h.kind)
    h.t, h.tmin, h.flags = c["t"].copy(), q.tmin.copy(), q.flags.copy()
    h.origin, h.direction, h.u, h.v = q.origin.copy(), q.direction.copy(), c["u"].copy(), c["v"].copy()
    h.procedural = q.status == 2
    if len(args) > 2 and isinstance(args[2], Pointer):
        pointer = args[2]
        rows = numpy.broadcast_to(pointer.rows, (n,))
        first = int(numpy.asarray(pointer.offset).ravel()[0])
        h.attributes = ([pointer.memory.types[k] for k in range(first, len(pointer.memory.cells))],
                        [pointer.memory.cells[k][rows].copy() for k in range(first, len(pointer.memory.cells))])
    if q.structure is not None and q.structure.instances:
        table = numpy.array([inst["contribution"] for inst in q.structure.instances])
        h.index = table[numpy.clip(h.instance, 0, len(table) - 1)] + h.geometry
    return h


def _hit_object_invoke(m, args, ins, lanes, active):
    """Invoke: the closest hit shader of the hit lanes, the miss shader of the miss lanes; nothing for a nop."""
    h, payload = args[0], args[1]
    rays = getattr(m, "rays", None)
    if not isinstance(h, HitObject) or rays is None:
        return None
    n = m.n
    o2w, w2o, o_origin, o_direction = h.object_space()
    hit = active & (h.state == 1)
    for index in numpy.unique(h.index[hit]):
        sel = hit & (h.index == index)
        found = rays.record(rays.desc["hitgroups"], int(index))
        if found is None or found[1] is None or found[1][0] != "hitgroup":
            continue
        name, (_, group), data = found
        export = rays.state.exports.get(group.get("closest")) if group.get("closest") else None
        if export is None or export[0] != "shader":
            continue
        ids = numpy.zeros(n, dtype=numpy.int64)
        if h.structure is not None and h.structure.instances:
            table = numpy.array([inst["id"] for inst in h.structure.instances])
            ids = table[numpy.clip(h.instance, 0, len(table) - 1)]
        system = dict(m.system)
        system.update(world_origin=h.origin, world_direction=h.direction, tmin=h.tmin, tcurrent=h.t, object_origin=o_origin,
                      object_direction=o_direction, object_to_world=o2w, world_to_object=w2o, instance_id=ids,
                      instance_index=h.instance, primitive_index=h.primitive, geometry_index=h.geometry, hit_kind=h.kind,
                      ray_flags=h.flags)
        attributes = _copy_attributes(n, *h.attributes) if h.procedural[sel].any() and h.attributes is not None \
            else _attributes(n, h.u, h.v)
        rays.invoke(export[1], export[2], n, system, sel, [payload, attributes], rays.bindings_of(name, data))
    missed = active & (h.state == 2)
    for index in numpy.unique(h.index[missed]):
        sel = missed & (h.index == index)
        found = rays.record(rays.desc["miss"], int(index))
        if found is None or found[1] is None or found[1][0] != "shader":
            continue
        system = dict(m.system)
        system.update(world_origin=h.origin, world_direction=h.direction, tmin=h.tmin, tcurrent=h.t, ray_flags=h.flags)
        rays.invoke(found[1][1], found[1][2], n, system, sel, [payload], rays.bindings_of(found[0], found[2]))
    return None


def _hit_state(value):
    def run(m, args, ins, lanes, active):
        h = args[0]
        return (h.state == value).astype(numpy.int64) if isinstance(h, HitObject) else numpy.zeros(m.n, dtype=numpy.int64)
    return run


def _hit_field(name):
    def run(m, args, ins, lanes, active):
        h = args[0]
        if not isinstance(h, HitObject):
            return numpy.zeros(m.n, dtype=numpy.float32 if ins.type.kind == "float" else numpy.int64)
        if name == "instance_id":
            values = numpy.zeros(m.n, dtype=numpy.int64)
            if h.structure is not None and h.structure.instances:
                table = numpy.array([inst["id"] for inst in h.structure.instances])
                values = table[numpy.clip(h.instance, 0, len(table) - 1)]
        else:
            values = getattr(h, name)
        if values.ndim == 2:
            values = values[:, int(numpy.asarray(args[1]).ravel()[0])]
        return values.astype(numpy.float32) if ins.type.kind == "float" else values.astype(numpy.int64)
    return run


def _hit_object_ray(which):
    def run(m, args, ins, lanes, active):
        h = args[0]
        if not isinstance(h, HitObject):
            return numpy.zeros(m.n, dtype=numpy.float32)
        _, _, origin, direction = h.object_space()
        return (origin if which == "origin" else direction)[:, int(numpy.asarray(args[1]).ravel()[0])].astype(numpy.float32)
    return run


def _hit_object_matrix(inverse):
    def run(m, args, ins, lanes, active):
        h = args[0]
        if not isinstance(h, HitObject):
            return numpy.zeros(m.n, dtype=numpy.float32)
        o2w, w2o, _, _ = h.object_space()
        row, column = int(numpy.asarray(args[1]).ravel()[0]), int(numpy.asarray(args[2]).ravel()[0])
        return (w2o if inverse else o2w)[:, row, column].astype(numpy.float32)
    return run


def _hit_object_set_index(m, args, ins, lanes, active):
    h = args[0]
    if not isinstance(h, HitObject):
        return h
    out = h.copy()
    out.index = numpy.where(active, numpy.broadcast_to(numpy.asarray(args[1], dtype=numpy.int64), (m.n,)), out.index)
    return out


def _hit_object_local_constant(m, args, ins, lanes, active):
    """LoadLocalRootTableConstant: a 32-bit value of the local root arguments of the hit object's record."""
    h = args[0]
    out = numpy.zeros(m.n, dtype=numpy.int64)
    rays = getattr(m, "rays", None)
    if not isinstance(h, HitObject) or rays is None:
        return out
    offset = numpy.broadcast_to(numpy.asarray(args[1], dtype=numpy.int64), (m.n,))
    for lane in numpy.nonzero(active & (h.state != 0))[0]:
        table = rays.desc["hitgroups"] if h.state[lane] == 1 else rays.desc["miss"]
        found = rays.record(table, int(h.index[lane]))
        if found is not None:
            at = 32 + int(offset[lane])
            data = found[2]
            out[lane] = struct.unpack_from("<I", data, at)[0] if at + 4 <= len(data) else 0
    return out


def _hit_object_attributes(m, args, ins, lanes, active):
    """HitObject::GetAttributes: the committed attributes written where the pointer points."""
    h, pointer = args[0], args[1]
    if not isinstance(h, HitObject) or not isinstance(pointer, Pointer):
        return None
    rows = numpy.broadcast_to(pointer.rows, (m.n,))
    first = int(numpy.asarray(pointer.offset).ravel()[0])
    cells = h.attributes[1] if h.attributes is not None and h.procedural.any() else [h.u, h.v]
    for k, cell in enumerate(cells):
        if first + k < len(pointer.memory.cells):
            target = pointer.memory.cells[first + k]
            target[rows[active]] = numpy.asarray(cell)[active].astype(target.dtype)
    return None


def _maybe_reorder(m, args, ins, lanes, active):
    return None


SHADER_EXECUTION_REORDERING = {
    262: _hit_object_trace, 263: _hit_object_from_query, 264: _hit_object_from_query, 265: _hit_object_miss,
    266: _hit_object_nop, 267: _hit_object_invoke, 268: _maybe_reorder, 269: _hit_state(2), 270: _hit_state(1),
    271: _hit_state(0), 272: _hit_field("flags"), 273: _hit_field("tmin"), 274: _hit_field("t"), 275: _hit_field("origin"),
    276: _hit_field("direction"), 277: _hit_object_ray("origin"), 278: _hit_object_ray("direction"),
    279: _hit_object_matrix(False), 280: _hit_object_matrix(True), 281: _hit_field("geometry"), 282: _hit_field("instance"),
    283: _hit_field("instance_id"), 284: _hit_field("primitive"), 285: _hit_field("kind"), 286: _hit_field("index"),
    287: _hit_object_set_index, 288: _hit_object_local_constant, 289: _hit_object_attributes,
}

# ------------------------------------------------------------------ the dx.op of ray tracing
def _vec(name):
    def run(m, args, ins, lanes, active):
        value = m.system.get(name)
        if value is None:
            return numpy.zeros(m.n, dtype=numpy.float32 if ins.type.kind in ("float", "half") else numpy.int64)
        component = int(numpy.asarray(args[0])[0]) if args else 0
        out = numpy.asarray(value)[:, component]
        return out.astype(numpy.float32) if ins.type.kind == "float" else out.astype(numpy.int64)
    return run


def _scalar(name):
    def run(m, args, ins, lanes, active):
        value = m.system.get(name)
        if value is None:
            return numpy.zeros(m.n, dtype=numpy.float32 if ins.type.kind == "float" else numpy.int64)
        out = numpy.broadcast_to(numpy.asarray(value), (m.n,))
        return out.astype(numpy.float32) if ins.type.kind == "float" else out.astype(numpy.int64)
    return run


def _matrix(name):
    def run(m, args, ins, lanes, active):
        value = m.system.get(name)
        row, column = int(numpy.asarray(args[0])[0]), int(numpy.asarray(args[1])[0])
        if value is None:
            return numpy.zeros(m.n, dtype=numpy.float32)
        return numpy.asarray(value)[:, row, column].astype(numpy.float32)
    return run


def _trace_ray(m, args, ins, lanes, active):
    return m.rays.trace(m, args, active)


def _report_hit(m, args, ins, lanes, active):
    traversal = getattr(m, "traversal", None)
    if traversal is None:
        return numpy.zeros(m.n, dtype=numpy.int64)
    return traversal.report(m, args[0], args[1], args[2], active)


def _ignore_hit(m, args, ins, lanes, active):
    m.ignored = getattr(m, "ignored", numpy.zeros(m.n, dtype=bool)) | active
    m.discarded |= active
    return None


def _accept_end(m, args, ins, lanes, active):
    m.accepted_end = getattr(m, "accepted_end", numpy.zeros(m.n, dtype=bool)) | active
    m.discarded |= active
    return None


def _call_shader(m, args, ins, lanes, active):
    index = numpy.asarray(args[0], dtype=numpy.int64)
    for value in numpy.unique(index[active]):
        found = m.rays.record(m.rays.desc["callables"], int(value))
        if found is not None and found[1] is not None and found[1][0] == "shader":
            m.rays.invoke(found[1][1], found[1][2], m.n, dict(m.system), active & (index == value), [args[1]],
                          m.rays.bindings_of(found[0], found[2]))
    return None


def _create_handle_for_lib(m, args, ins, lanes, active):
    return args[0]


CALLBACKS = {145: _vec("rays_index"), 146: _vec("rays_dims"), 147: _vec("world_origin"), 148: _vec("world_direction"),
             149: _vec("object_origin"), 150: _vec("object_direction"), 151: _matrix("object_to_world"),
             152: _matrix("world_to_object"), 153: _scalar("tmin"), 154: _scalar("tcurrent"), 141: _scalar("instance_id"),
             142: _scalar("instance_index"), 143: _scalar("hit_kind"), 144: _scalar("ray_flags"), 161: _scalar("primitive_index"),
             213: _scalar("geometry_index"), 157: _trace_ray, 155: _ignore_hit, 156: _accept_end, 159: _call_shader,
             158: _report_hit}
CALLBACKS.update(SHADER_EXECUTION_REORDERING)

# RayQuery and createHandleForLib work in every shader: in dxil's table
RAY_QUERY = {
    160: _create_handle_for_lib, 178: _allocate_query, 258: _allocate_query, 179: _trace_inline, 180: _proceed, 181: _abort,
    182: _commit_triangle, 183: _commit_procedural, 184: _query_status,
    185: _query_value("candidate", "kind"),
    186: _query_matrix("candidate", False), 187: _query_matrix("candidate", True),
    188: _query_matrix("committed", False), 189: _query_matrix("committed", True),
    190: _query_candidate_non_opaque, 191: _query_front("candidate"), 192: _query_front("committed"),
    193: _query_barycentrics("candidate"), 194: _query_barycentrics("committed"),
    195: _query_ray("flags"), 196: _query_ray("origin"), 197: _query_ray("direction"), 198: _query_ray("tmin"),
    199: _query_value("candidate", "t"), 200: _query_value("committed", "t"),
    201: _query_value("candidate", "instance"), 202: _query_instance("candidate", "id"),
    203: _query_value("candidate", "geometry"), 204: _query_value("candidate", "primitive"),
    205: _query_object_ray("candidate", False), 206: _query_object_ray("candidate", True),
    207: _query_value("committed", "instance"), 208: _query_instance("committed", "id"),
    209: _query_value("committed", "geometry"), 210: _query_value("committed", "primitive"),
    211: _query_object_ray("committed", False), 212: _query_object_ray("committed", True),
    214: _query_instance("candidate", "contribution"), 215: _query_instance("committed", "contribution"),
}


def _register():
    from .dxil import _DX
    _DX.update(RAY_QUERY)


_register()
