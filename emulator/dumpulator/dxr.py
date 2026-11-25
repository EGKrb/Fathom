# Fathom addition to dumpulator: the ray tracing of Direct3D 12 (DXR 1.0) for d3d12_emu.py: state objects
# (DXIL libraries, hit groups), acceleration structures (triangles; instances with their transforms, masks
# and flags) and DispatchRays, whose shaders run in dxil.py. Boost Software License 1.0, like the rest of
# dumpulator (LICENSE.dumpulator).
#
# DispatchRays runs the ray generation shader on all the rays at once; TraceRay intersects the rays of the
# active lanes with the triangles of the instances (numpy: every ray against every triangle, Moller-
# Trumbore, in object space), takes the candidates nearest first (an any hit shader decides for the non
# opaque ones: IgnoreHit, AcceptHitAndEndSearch), then runs the closest hit shader of each lane's hit group
# (or the miss shader) on those lanes, with the payload of the caller (shared memory: same lanes) and the
# barycentrics. Shader records are read from the shader tables in memory (their first 32 bytes: the
# identifier). Not covered: procedural geometry (intersection shaders, AABBs), local root signatures,
# inline ray tracing (RayQuery).

import struct
from typing import Dict, List, Optional

import numpy

from .dxil import Handle, Machine, Memory, Pointer
from .dxil_bitcode import Type

RAY_FLAG_FORCE_OPAQUE, RAY_FLAG_FORCE_NON_OPAQUE = 0x1, 0x2
RAY_FLAG_ACCEPT_FIRST_HIT, RAY_FLAG_SKIP_CLOSEST_HIT = 0x4, 0x8
RAY_FLAG_CULL_BACK, RAY_FLAG_CULL_FRONT, RAY_FLAG_CULL_OPAQUE, RAY_FLAG_CULL_NON_OPAQUE = 0x10, 0x20, 0x40, 0x80
HIT_KIND_FRONT, HIT_KIND_BACK = 254, 255


class Structure:
    """An acceleration structure: bottom (geometries: triangles (k, 3, 3), flags) or top (instances)."""

    def __init__(self, kind):
        self.kind = kind
        self.geometries: List[dict] = []
        self.instances: List[dict] = []


class StateObject:
    """ID3D12StateObject / ID3D12StateObjectProperties: the exports (name -> ("shader", library, function) or
    ("hitgroup", dict)) and their identifiers."""
    interface = "ID3D12StateObject"
    interfaces = ["ID3D12StateObjectProperties"]

    def __init__(self, d3d):
        self.d3d = d3d
        self.exports: Dict[str, tuple] = {}
        self.identifiers: Dict[bytes, str] = {}
        self.addresses: Dict[str, int] = {}
        self.global_root = None
        self.resources: Dict[int, dict] = {}    # the declared resources of its libraries (root SRV/UAV strides)
        self.uavs: Dict[int, dict] = {}

    def identifier(self, name: str) -> int:
        if name not in self.exports:
            return 0
        if name not in self.addresses:
            ident = b"FATHOMRT" + struct.pack("<I", len(self.addresses)) + bytes(20)
            dp = self.d3d.dp
            address = dp.allocate(32)
            dp.write(address, ident)
            self.addresses[name] = address
            self.identifiers[ident] = name
        return self.addresses[name]

    def GetShaderIdentifier(self, name):
        return self.identifier(_wide(self.d3d.dp, name))

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

    def record(self, data: bytes):
        """The export of a shader record (its identifier)."""
        name = self.identifiers.get(bytes(data[:32]))
        return self.exports.get(name) if name else None


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


def create_state_object(d3d, desc_address) -> StateObject:
    """A state object from a D3D12_STATE_OBJECT_DESC: its DXIL libraries and hit groups."""
    from .dxil import Shader
    dp = d3d.dp
    ptr = dp.ptr_size()
    kind, count = struct.unpack("<2I", bytes(dp.read(desc_address, 8)))
    subobjects = dp.read_ptr(desc_address + 8)
    state = StateObject(d3d)
    size = 2 * ptr if ptr == 8 else 8
    hitgroups = []
    for k in range(count):
        base = subobjects + k * size
        sub_type = struct.unpack("<I", bytes(dp.read(base, 4)))[0]
        desc = dp.read_ptr(base + (8 if ptr == 8 else 4))
        if sub_type == 5:       # DXIL_LIBRARY: bytecode, exports
            code, length = dp.read_ptr(desc), dp.read_ptr(desc + ptr)
            exports = struct.unpack("<I", bytes(dp.read(desc + 2 * ptr, 4)))[0]
            table = dp.read_ptr(desc + 3 * ptr if ptr == 8 else desc + 12)
            library = Shader(bytes(dp.read(code, length)))
            state.resources.update(library.resources)
            state.uavs.update(library.uavs)
            names = {}
            for e in range(exports):
                entry = table + e * (3 * ptr if ptr == 8 else 12)
                name = _wide(dp, dp.read_ptr(entry))
                renamed = _wide(dp, dp.read_ptr(entry + ptr))
                names[name] = renamed or name
            for export, function in _library_exports(library).items():
                if not names:
                    state.exports[export] = ("shader", library, function)
            for name, source in names.items():
                function = _library_exports(library).get(source)
                if function is not None:
                    state.exports[name] = ("shader", library, function)
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
    for name, group in hitgroups:
        state.exports[name] = ("hitgroup", group)
    return state


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
        transform, index_format, vertex_format, index_count, vertex_count, indices, vertices, stride = \
            struct.unpack("<Q4I3Q", bytes(dp.read(at + 8, 48)))
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
        result = 256 + sum(64 * max(g["index_count"] or g["vertex_count"], 1) // 3 + 64 for g in desc["geometries"])
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
            if g["type"] != 0:
                structure.geometries.append(dict(kind="aabbs", flags=g["flags"], triangles=numpy.zeros((0, 3, 3))))
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


# ------------------------------------------------------------------ dispatching
class Rays:
    """The run of a DispatchRays: the state object, the shader tables, the bindings, the structures."""

    def __init__(self, d3d, state: StateObject, desc: dict, bindings):
        self.d3d, self.state, self.desc, self.bindings = d3d, state, desc, bindings

    def record(self, table, index):
        start, size, stride = table
        if not start:
            return None
        found = self.d3d.at_address(start + index * stride)
        if found is None:
            return None
        resource, offset = found
        return self.state.record(resource.read()[offset:offset + 32].tobytes())

    def run(self):
        desc = self.desc
        width, height, depth = desc["width"], desc["height"], desc["depth"]
        n = width * height * depth
        if not n:
            return
        export = self.record((desc["raygen"][0], desc["raygen"][1], 0), 0)
        if export is None or export[0] != "shader":
            return
        _, library, function = export
        lane = numpy.arange(n)
        index = numpy.stack([lane % width, (lane // width) % height, lane // (width * height)], 1)
        system = {"rays_index": index, "rays_dims": numpy.tile([width, height, depth], (n, 1))}
        self._invoke(library, function, n, system, numpy.ones(n, dtype=bool), [])

    def _invoke(self, library, function, n, system, mask, args):
        machine = Machine(library, function, n, self.bindings, system=system)
        machine.callbacks = CALLBACKS
        machine.rays = self
        machine.ended = numpy.zeros(n, dtype=bool)
        machine.run(mask, args)
        return machine

    # TraceRay ------------------------------------------------------------------------------------
    def trace(self, m: Machine, args, lanes):
        n = m.n
        structure = self._structure(m, args[0])
        flags = int(numpy.asarray(args[1])[0]) if isinstance(args[1], numpy.ndarray) else 0
        include = numpy.asarray(args[2], dtype=numpy.int64)
        contribution = numpy.asarray(args[3], dtype=numpy.int64)
        multiplier = numpy.asarray(args[4], dtype=numpy.int64)
        miss_index = numpy.asarray(args[5], dtype=numpy.int64)
        origin = numpy.stack([numpy.asarray(a, dtype=numpy.float64) for a in args[6:9]], 1)
        tmin = numpy.asarray(args[9], dtype=numpy.float64)
        direction = numpy.stack([numpy.asarray(a, dtype=numpy.float64) for a in args[10:13]], 1)
        tmax = numpy.asarray(args[13], dtype=numpy.float64)
        payload = args[14]
        hit = numpy.zeros(n, dtype=bool)
        best_t = tmax.copy()
        found = dict(instance=numpy.full(n, -1), geometry=numpy.zeros(n, numpy.int64), primitive=numpy.zeros(n, numpy.int64),
                     u=numpy.zeros(n), v=numpy.zeros(n), front=numpy.ones(n, dtype=bool))
        candidates = []     # (t (n, k), instance, geometry, front, u, v, opaque)
        for i, instance in enumerate(structure.instances if structure is not None else []):
            visible = lanes & ((instance["mask"] & include) != 0)
            if not visible.any():
                continue
            blas = self.d3d.structures.get(instance["blas"])
            if blas is None:
                continue
            m34 = instance["transform"]
            full = numpy.vstack([m34, [0, 0, 0, 1]])
            inverse = numpy.linalg.inv(full)[:3]
            o = origin @ inverse[:, :3].T + inverse[:, 3]
            d = direction @ inverse[:, :3].T
            for g, geometry in enumerate(blas.geometries):
                tris = geometry["triangles"]
                if not len(tris):
                    continue
                t, u, v, front = _intersect(o, d, tris)
                instance_flags = instance["flags"]
                if instance_flags & 2:          # FRONT_COUNTERCLOCKWISE
                    front = ~front
                ok = numpy.isfinite(t) & (t > tmin[:, None]) & (t < tmax[:, None]) & visible[:, None]
                if not instance_flags & 1:      # culling unless TRIANGLE_CULL_DISABLE
                    if flags & RAY_FLAG_CULL_BACK:
                        ok &= front
                    if flags & RAY_FLAG_CULL_FRONT:
                        ok &= ~front
                opaque = bool(geometry["flags"] & 1)
                if instance_flags & 4:
                    opaque = True
                if instance_flags & 8:
                    opaque = False
                if flags & RAY_FLAG_FORCE_OPAQUE:
                    opaque = True
                if flags & RAY_FLAG_FORCE_NON_OPAQUE:
                    opaque = False
                if (flags & RAY_FLAG_CULL_OPAQUE and opaque) or (flags & RAY_FLAG_CULL_NON_OPAQUE and not opaque):
                    continue
                candidates.append((numpy.where(ok, t, numpy.inf), i, g, front, u, v, opaque))
        # nearest first: an opaque candidate commits; a non opaque one asks its any hit shader
        searching = lanes.copy()
        considered = [numpy.zeros_like(c[0], dtype=bool) for c in candidates]
        while candidates and searching.any():
            best = numpy.full(n, numpy.inf)
            which = numpy.full(n, -1)
            column = numpy.zeros(n, dtype=numpy.int64)
            for k, (t, *_rest) in enumerate(candidates):
                masked = numpy.where(considered[k], numpy.inf, t)
                j = masked.argmin(axis=1)
                tj = masked[numpy.arange(n), j]
                better = (tj < best) & (tj < best_t) & searching
                best = numpy.where(better, tj, best)
                which = numpy.where(better, k, which)
                column = numpy.where(better, j, column)
            have = searching & (which >= 0)
            if not have.any():
                break
            for k, (t, i, g, front, u, v, opaque) in enumerate(candidates):
                sel = have & (which == k)
                if not sel.any():
                    continue
                rows = numpy.nonzero(sel)[0]
                considered[k][rows, column[rows]] = True
                accept = sel.copy()
                end = numpy.zeros(n, dtype=bool)
                if not opaque:
                    group = self._hitgroup(structure.instances[i], g, contribution, multiplier, sel)
                    for name_group, lanes_group in group:
                        if name_group is not None and name_group.get("anyhit"):
                            ex = self.state.exports.get(name_group["anyhit"])
                            if ex is not None and ex[0] == "shader":
                                system = self._hit_system(m, structure.instances[i], g, column, best, front, k, i, origin,
                                                          direction, tmin, flags)
                                attributes = _attributes(n, u[numpy.arange(n), column], v[numpy.arange(n), column])
                                machine = self._invoke(ex[1], ex[2], n, system, lanes_group, [payload, attributes])
                                accept &= ~(lanes_group & getattr(machine, "ignored", numpy.zeros(n, dtype=bool)))
                                end |= lanes_group & getattr(machine, "accepted_end", numpy.zeros(n, dtype=bool))
                commit = accept
                hit |= commit
                best_t = numpy.where(commit, best, best_t)
                found["instance"] = numpy.where(commit, i, found["instance"])
                found["geometry"] = numpy.where(commit, g, found["geometry"])
                found["primitive"] = numpy.where(commit, column, found["primitive"])
                cols = numpy.clip(column, 0, u.shape[1] - 1)
                found["u"] = numpy.where(commit, u[numpy.arange(n), cols], found["u"])
                found["v"] = numpy.where(commit, v[numpy.arange(n), cols], found["v"])
                found["front"] = numpy.where(commit, front[numpy.arange(n), cols], found["front"])
                stop = commit & ((flags & RAY_FLAG_ACCEPT_FIRST_HIT) != 0) | end
                searching &= ~stop
            # a committed hit bounds the search: the candidates beyond it are not nearer
            searching &= numpy.isfinite(best)
        # closest hit / miss
        if hit.any() and not flags & RAY_FLAG_SKIP_CLOSEST_HIT:
            for i in numpy.unique(found["instance"][hit]):
                instance = structure.instances[int(i)]
                for g in numpy.unique(found["geometry"][hit & (found["instance"] == i)]):
                    sel = hit & (found["instance"] == i) & (found["geometry"] == g)
                    for group, lanes_group in self._hitgroup(instance, int(g), contribution, multiplier, sel):
                        if group is None or not group.get("closest"):
                            continue
                        ex = self.state.exports.get(group["closest"])
                        if ex is None or ex[0] != "shader":
                            continue
                        system = self._hit_system(m, instance, int(g), found["primitive"], best_t, found["front"], None, int(i),
                                                  origin, direction, tmin, flags)
                        attributes = _attributes(n, found["u"], found["v"])
                        self._invoke(ex[1], ex[2], n, system, lanes_group, [payload, attributes])
        missed = lanes & ~hit
        if missed.any():
            for index in numpy.unique(miss_index[missed]):
                sel = missed & (miss_index == index)
                ex = self.record(self.desc["miss"], int(index))
                if ex is None or ex[0] != "shader":
                    continue
                system = dict(m.system)
                system.update(world_origin=origin, world_direction=direction, tmin=tmin, tcurrent=tmax, ray_flags=numpy.full(n, flags))
                self._invoke(ex[1], ex[2], n, system, sel, [payload])
        return None

    def _structure(self, m, handle):
        view = m._view(handle) if isinstance(handle, Handle) else None
        address = getattr(view, "address", None) if view is not None else None
        return self.d3d.structures.get(address) if address is not None else None

    def _hitgroup(self, instance, geometry, contribution, multiplier, lanes):
        """[(hit group, lanes)] of the lanes: the record contribution + multiplier * geometry + the instance's."""
        out = []
        index = contribution + multiplier * geometry + instance["contribution"]
        for value in numpy.unique(index[lanes]):
            ex = self.record(self.desc["hitgroups"], int(value))
            group = ex[1] if ex is not None and ex[0] == "hitgroup" else None
            out.append((group, lanes & (index == value)))
        return out

    def _hit_system(self, m, instance, geometry, primitive, t, front, k, index, origin, direction, tmin, flags):
        n = m.n
        system = dict(m.system)
        full = numpy.vstack([instance["transform"], [0, 0, 0, 1]])
        inverse = numpy.linalg.inv(full)[:3]
        front_values = front if front.ndim == 1 else front[numpy.arange(n), numpy.clip(primitive, 0, front.shape[1] - 1)]
        system.update(world_origin=origin, world_direction=direction, tmin=tmin, tcurrent=numpy.asarray(t, dtype=numpy.float64),
                      object_origin=origin @ inverse[:, :3].T + inverse[:, 3], object_direction=direction @ inverse[:, :3].T,
                      object_to_world=numpy.broadcast_to(instance["transform"], (n, 3, 4)),
                      world_to_object=numpy.broadcast_to(inverse, (n, 3, 4)),
                      instance_id=numpy.full(n, instance["id"]), instance_index=numpy.full(n, index),
                      primitive_index=numpy.asarray(primitive), geometry_index=numpy.full(n, geometry),
                      hit_kind=numpy.where(front_values, HIT_KIND_FRONT, HIT_KIND_BACK), ray_flags=numpy.full(n, flags))
        return system


def _attributes(n, u, v):
    memory = Memory(Type("struct", elements=[Type("float", 32), Type("float", 32)]), n)
    memory.cells[0][:] = u
    memory.cells[1][:] = v
    return Pointer(memory, numpy.zeros(n, dtype=numpy.int64), numpy.arange(n))


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
    front = det > 0
    return t, u, v, front


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
        ex = m.rays.record(m.rays.desc["callables"], int(value))
        if ex is not None and ex[0] == "shader":
            m.rays._invoke(ex[1], ex[2], m.n, dict(m.system), active & (index == value), [args[1]])
    return None


def _create_handle_for_lib(m, args, ins, lanes, active):
    return args[0]


CALLBACKS = {145: _vec("rays_index"), 146: _vec("rays_dims"), 147: _vec("world_origin"), 148: _vec("world_direction"),
             149: _vec("object_origin"), 150: _vec("object_direction"), 151: _matrix("object_to_world"),
             152: _matrix("world_to_object"), 153: _scalar("tmin"), 154: _scalar("tcurrent"), 141: _scalar("instance_id"),
             142: _scalar("instance_index"), 143: _scalar("hit_kind"), 144: _scalar("ray_flags"), 161: _scalar("primitive_index"),
             213: _scalar("geometry_index"), 157: _trace_ray, 155: _ignore_hit, 156: _accept_end, 159: _call_shader,
             160: _create_handle_for_lib}
