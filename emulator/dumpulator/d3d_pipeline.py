# Fathom addition to dumpulator: the programmable pipeline of the emulated Direct3D 10 / 11 / 12: input
# assembly, vertex shader, hull shader + tessellator + domain shader, geometry shader, rasterization
# (raster.py: clipping, culling, interpolation), pixel shader, output merger with several render
# targets; stream output (the vertices of the last stage before the rasterizer written to buffers, per
# stream); and the dispatch of compute shaders. The shaders run in dxbc.py. Boost Software License 1.0,
# like the rest of dumpulator (LICENSE.dumpulator).

from typing import Dict, List, Optional

from . import dxbc
from .gpu import Image, fetch_attribute
from .raster import Output, Pipeline, Target, draw, numpy, primitives


class DrawState:
    """What a draw uses. shaders: stage ("vs", "hs", "ds", "gs", "ps") -> dxbc.Shader; bindings: stage ->
    dxbc.Bindings; layout: input elements (dicts name, index, kind, slot, offset, instanced, step);
    vertex_buffers: slot -> (bytes array, stride, offset); topology: Direct3D's number; targets: Images
    (None for an unbound slot); depth: Image; blends: raster.Blend per target; depth_state: (enabled,
    write, function); rasterizer: (fill, cull, front_ccw, depth_clip, scissor)."""

    def __init__(self):
        self.shaders: Dict[str, object] = {}
        self.bindings: Dict[str, object] = {}
        self.layout: List[dict] = []
        self.vertex_buffers: Dict[int, tuple] = {}
        self.topology = 4
        self.viewports: List[tuple] = []
        self.scissors: List[tuple] = []
        self.targets: List[Optional[Image]] = []
        self.depth: Optional[Image] = None
        self.blends: List[object] = []
        self.blend_factor = (1.0, 1.0, 1.0, 1.0)
        self.depth_state = (True, True, 2)
        self.rasterizer = (3, 3, False, True, False)
        self.srgb = set()          # the targets written in sRGB
        self.stream_out: Optional[StreamOutput] = None


class SOTarget:
    """A buffer bound for stream output: its bytes (numpy uint8, written in place), the offset where the next
    vertex goes and the size."""

    def __init__(self, data, offset: int, size: int, owner=None):
        self.data, self.offset, self.size, self.owner = data, offset, size, owner
        self.changed = False


class StreamOutput:
    """The stream output of a draw: declaration entries (stream, semantic name or None for a gap, semantic
    index, first component, component count, output slot), the stride of each slot, the stream that goes on
    to the rasterizer (-1: none), the targets (SOTarget per slot) and the statistics per stream (primitives
    written, primitives the buffers would have needed)."""

    def __init__(self, entries, strides, rasterized=0):
        self.entries, self.rasterized = list(entries), rasterized
        self.strides = list(strides) + [0] * (4 - len(strides))
        for slot in range(4):   # strides not given: the components declared for the slot
            if not self.strides[slot]:
                self.strides[slot] = 4 * sum(e[4] for e in self.entries if e[5] == slot)
        self.targets: List[Optional[SOTarget]] = [None] * 4
        self.written: Dict[int, int] = {}
        self.needed: Dict[int, int] = {}

    def streams(self):
        return sorted({e[0] for e in self.entries})


TOPOLOGIES = {1: "points", 2: "lines", 3: "line_strip", 4: "triangles", 5: "strip", 10: "lines_adj",
              11: "line_strip_adj", 12: "triangles_adj", 13: "strip_adj"}


def assemble(topology: int, count: int):
    """The primitives (p, k) of a topology over count vertices (adjacency kept for a geometry shader) and
    their kind (points, lines, triangles, lines_adj, triangles_adj, patches)."""
    if 33 <= topology <= 64:  # patch lists
        size = topology - 32
        n = count // size
        return numpy.arange(n * size).reshape(n, size), "patches"
    kind = TOPOLOGIES.get(topology, "triangles")
    i = numpy.arange(count)
    if kind == "lines_adj":
        n = count // 4
        return i[:4 * n].reshape(n, 4), "lines_adj"
    if kind == "line_strip_adj":
        n = max(0, count - 3)
        return numpy.stack([numpy.arange(n) + k for k in range(4)], 1), "lines_adj"
    if kind == "triangles_adj":
        n = count // 6
        return i[:6 * n].reshape(n, 6), "triangles_adj"
    if kind == "strip_adj":
        n = max(0, (count - 4) // 2)
        out = []
        for t in range(n):
            a, b, c = 2 * t, 2 * t + 2, 2 * t + 4
            if t % 2 == 0:
                out.append((a, a + 1, b, min(c + 1, count - 1), c, b + 1))
            else:
                out.append((b, a + 1, a, min(c + 1, count - 1), c, b + 1))
        return numpy.array(out, dtype=numpy.int64).reshape(-1, 6), "triangles_adj"
    prims = primitives(kind, count)
    return prims, {"points": "points", "lines": "lines", "line_strip": "lines"}.get(kind, "triangles")


def vertex_inputs(state: DrawState, shader, vertex_ids, instance, fetch_instance=None):
    """The input registers of a vertex shader: the input layout reading the vertex buffers."""
    n = len(vertex_ids)
    inputs = {}
    ids = numpy.asarray(vertex_ids, dtype=numpy.int64)
    for element in shader.inputs:
        register = element["register"]
        if element["sv"] == 6:  # SV_VertexID
            value = numpy.repeat(ids.astype(numpy.uint32)[:, None], 4, axis=1)
        elif element["sv"] == 8:  # SV_InstanceID
            value = numpy.full((n, 4), instance, dtype=numpy.uint32)
        else:
            match = next((e for e in state.layout if e["name"] == element["name"] and e["index"] == element["index"]), None)
            value = numpy.zeros((n, 4), dtype=numpy.float32)
            value[:, 3] = 1
            value = value.view(numpy.uint32)
            if match is not None:
                entry = state.vertex_buffers.get(match["slot"])
                if entry is not None and entry[0] is not None:
                    data, stride, offset = entry
                    fetch = instance if fetch_instance is None else fetch_instance
                    rows = ids if not match["instanced"] else numpy.full(n, fetch // max(1, match["step"] or 1))
                    value = fetch_attribute(data, offset + match["offset"], stride, rows, match["kind"])
        if register in inputs:  # elements packed in one register
            for c in range(4):
                if element["mask"] >> c & 1:
                    inputs[register][:, c] = value[:, c]
        else:
            inputs[register] = numpy.array(value, dtype=numpy.uint32)
    return inputs


def _by_semantic(signature_list, name, index):
    for element in signature_list:
        if element["name"] == name and element["index"] == index and element.get("stream", 0) == 0:
            return element
    return None


def _position_register(signature_list):
    for element in signature_list:
        if element["sv"] == 1 and element.get("stream", 0) == 0:
            return element["register"]
    return None


def _tess_factors(hs, constants, patch):
    """The edge and inside factors of a patch, from the patch constants (the PCSG signature)."""
    edges, inside = {}, {}
    for element in hs.patch:
        values = constants.get(element["register"])
        if values is None:
            continue
        component = (element["mask"] & -element["mask"]).bit_length() - 1
        value = float(values[patch].view(numpy.float32)[component])
        if element["sv"] in (11, 13, 15):
            edges[element["index"]] = value
        elif element["sv"] in (12, 14, 16):
            inside[element["index"]] = value
    return [edges[k] for k in sorted(edges)], [inside[k] for k in sorted(inside)]


def draw_dxbc(state: DrawState, vertex_ids, instances=(0,)) -> int:
    """A draw (Draw, DrawIndexed and their instanced forms): the number of fragments written."""
    vs, hs, ds, gs, ps = (state.shaders.get(k) for k in ("vs", "hs", "ds", "gs", "ps"))
    if vs is None or len(vertex_ids) == 0 or not state.viewports and state.stream_out is None:
        return 0
    empty = dxbc.Bindings()
    written = 0
    for instance in instances:
        sv_instance, fetch_instance = instance if isinstance(instance, tuple) else (instance, instance)
        unique, inverse = numpy.unique(numpy.asarray(vertex_ids, dtype=numpy.int64), return_inverse=True)
        result = dxbc.run_vertex(vs, len(unique), vertex_inputs(state, vs, unique, sv_instance, fetch_instance),
                                 state.bindings.get("vs", empty))
        outputs, signature_list = result.outputs, vs.outputs
        prims, kind = assemble(state.topology, len(inverse))
        prims = inverse[prims].astype(numpy.int64) if len(prims) else prims.astype(numpy.int64)
        primitive_id = numpy.arange(len(prims), dtype=numpy.uint32)
        if kind == "patches":
            if hs is None or ds is None:
                continue
            outputs, prims, kind, signature_list, primitive_id = _tessellate(state, hs, ds, outputs, prims)
        so = state.stream_out
        rasterized = so.rasterized if so is not None else 0
        if gs is not None and len(prims):
            streams = sorted(set(so.streams() if so is not None else []) | ({rasterized} if rasterized >= 0 else set()))
            per_stream = _geometry_streams(state, gs, outputs, prims, primitive_id, streams or [0])
            if so is not None:
                for stream in so.streams():
                    if stream in per_stream:
                        s_outputs, s_prims, s_kind, _ = per_stream[stream]
                        stream_out(so, stream, s_outputs, s_prims, [e for e in gs.outputs if e.get("stream", 0) == stream])
            if rasterized < 0 or rasterized not in per_stream:
                continue
            outputs, prims, kind, primitive_id = per_stream[rasterized]
            signature_list = [e for e in gs.outputs if e.get("stream", 0) == rasterized]
        else:
            if kind in ("lines_adj", "triangles_adj"):
                prims = prims[:, [1, 2]] if kind == "lines_adj" else prims[:, [0, 2, 4]]
                kind = "lines" if kind == "lines_adj" else "triangles"
            if so is not None:
                stream_out(so, 0, outputs, prims, signature_list)
                if rasterized != 0:
                    continue
        if len(prims) == 0 or not outputs or not state.viewports:
            continue
        written += _rasterize(state, ps, outputs, signature_list, prims, kind, primitive_id)
    return written


PRIMITIVE_REGISTERS = 64      # the per-primitive outputs of a mesh shader, after its vertex outputs


def draw_mesh(state: DrawState, groups) -> int:
    """DispatchMesh: the amplification shader's groups (each launching mesh groups with its payload), or the mesh
    groups directly; their vertices and primitives rasterized (per-primitive attributes constant over their
    primitive, SV_CullPrimitive, SV_PrimitiveID). The number of fragments written."""
    from . import dxil_ops
    amplification, ms, ps = (state.shaders.get(k) for k in ("as", "ms", "ps"))
    if ms is None or not state.viewports:
        return 0
    empty = dxbc.Bindings()
    launches = []
    if amplification is not None:
        for counts, payload in dxil_ops.run_amplification(amplification, groups, state.bindings.get("as", empty)):
            if all(counts):
                launches.append((counts, payload))
    else:
        launches.append((tuple(groups), None))
    topology = (ms.mesh or (0, 0, 2))[2]
    kind = "lines" if topology == 1 else "triangles"
    signature = list(ms.outputs) + [dict(e, register=e["register"] + PRIMITIVE_REGISTERS) for e in ms.patch]
    written = 0
    for counts, payload in launches:
        vertices, primitives, prims = dxil_ops.run_mesh(ms, counts, state.bindings.get("ms", empty), payload)
        if len(prims) == 0 or not vertices:
            continue
        prims = prims[:, :2] if kind == "lines" else prims
        primitive_id = numpy.arange(len(prims), dtype=numpy.uint32)
        keep = numpy.ones(len(prims), dtype=bool)
        for element in ms.patch:
            values = primitives.get(element["register"])
            if values is None:
                continue
            component = (element["mask"] & -element["mask"]).bit_length() - 1 if element["mask"] else 0
            if element["name"] == "SV_PRIMITIVEID":
                primitive_id = values[:, component].astype(numpy.uint32)
            elif element["name"] == "SV_CULLPRIMITIVE":
                keep &= values[:, component] == 0
        if primitives:          # each primitive gets its own vertices: its attributes are constant over it
            flat = prims.reshape(-1)
            per = prims.shape[1]
            outputs = {reg: values[flat] for reg, values in vertices.items()}
            for reg, values in primitives.items():
                outputs[reg + PRIMITIVE_REGISTERS] = numpy.repeat(values, per, axis=0)
            prims = numpy.arange(len(flat), dtype=numpy.int64).reshape(-1, per)
        else:
            outputs = vertices
        prims, primitive_id = prims[keep], primitive_id[keep]
        if len(prims):
            written += _rasterize(state, ps, outputs, signature, prims, kind, primitive_id)
    return written


def stream_out(so: StreamOutput, stream: int, outputs, prims, signature_list):
    """Writes the vertices of the primitives of a stream (lists: strips are already split) to the buffers of
    the stream output, as long as every buffer of the stream has room for a whole primitive."""
    entries = [e for e in so.entries if e[0] == stream]
    count = len(prims)
    so.needed[stream] = so.needed.get(stream, 0) + count
    so.written.setdefault(stream, 0)
    if not entries or count == 0:
        return
    per_prim = prims.shape[1]
    slots = sorted({e[5] for e in entries})
    limit = count
    for slot in slots:
        target, stride = so.targets[slot], so.strides[slot]
        if target is not None and stride:
            limit = min(limit, max(0, (target.size - target.offset) // (stride * per_prim)))
    so.written[stream] += limit
    if limit <= 0:
        return
    vertices = prims[:limit].reshape(-1)
    for slot in slots:
        target, stride = so.targets[slot], so.strides[slot]
        if target is None or not stride:
            continue
        words = stride // 4
        start, end = target.offset, target.offset + len(vertices) * stride
        existing = numpy.zeros(len(vertices) * stride, dtype=numpy.uint8)
        have = target.data[start:min(end, len(target.data))]
        existing[:len(have)] = have
        rows = existing.view(numpy.uint32).reshape(len(vertices), words).copy()
        column = 0
        for _, name, index, first, components, entry_slot in entries:
            if entry_slot != slot:
                continue
            if name is not None:
                element = next((e for e in signature_list if e["name"].upper() == name.upper() and e["index"] == index), None)
                if element is not None and element["register"] in outputs:
                    base = (element["mask"] & -element["mask"]).bit_length() - 1 if element["mask"] else 0
                    values = outputs[element["register"]][vertices]
                    take = values[:, base + first:base + first + components]
                    rows[:, column:column + take.shape[1]] = take
            column += components
        target.data[start:end] = rows.view(numpy.uint8).reshape(-1)[:max(0, min(end, len(target.data)) - start)]
        target.offset = end
        target.changed = True


def _rasterize(state, ps, outputs, signature_list, prims, kind, primitive_id):
    empty = dxbc.Bindings()
    position = _position_register(signature_list)
    if position is None or position not in outputs:
        return 0
    n_vertices = len(outputs[position])
    clip = outputs[position].view(numpy.float32).astype(numpy.float64)
    # the attributes the pixel shader reads, from the last stage's outputs
    links, columns, flat = [], [], []
    if ps is not None:
        for element in ps.inputs:
            if element["sv"] in (1, 7, 9, 10):
                continue
            source = _by_semantic(signature_list, element["name"], element["index"])
            values = outputs.get(source["register"]) if source is not None else None
            integer = element["kind"] in (1, 2)
            if values is None:
                values = numpy.zeros((n_vertices, 4), dtype=numpy.uint32)
            column = values.astype(numpy.float64) if integer else values.view(numpy.float32).astype(numpy.float64)
            links.append((element, len(columns) * 4, integer))
            columns.append(column)
            flat.append(ps.interpolation.get(element["register"], 2) == 1 or integer)
    attrs = numpy.concatenate(columns, axis=1) if columns else numpy.zeros((n_vertices, 0))
    flat_mask = numpy.repeat(numpy.array(flat, dtype=bool), 4) if flat else None
    fill, cull, front_ccw, depth_clip, scissor_on = state.rasterizer
    x, y, w, h, near, far = state.viewports[0]
    scissor = state.scissors[0] if scissor_on and state.scissors else None
    pipeline = Pipeline((x, y, w, h), "d3d", (near, far), cull={1: "none", 2: "front", 3: "back"}.get(cull, "back"),
                        front_cw=not front_ccw, provoking="first", scissor=scissor, clip_depth=bool(depth_clip))
    targets = []
    for index, image in enumerate(state.targets):
        blend = state.blends[index] if index < len(state.blends) else (state.blends[0] if state.blends else None)
        targets.append(Target(image.color, blend, srgb=index in state.srgb) if image is not None else None)
        if image is not None:
            image.changed()
    enabled, write, function = state.depth_state
    depth = state.depth.fb.depth if state.depth is not None else None
    first = next((t for t in targets if t is not None), None)
    if depth is not None and first is not None and depth.shape != first.color.shape[:2]:
        depth = None
    if state.depth is not None:
        state.depth.changed()
    output = Output(targets, depth, function if enabled and depth is not None else 0, bool(write and enabled),
                    state.blend_factor)
    if first is None and depth is not None:
        output.height, output.width = depth.shape
    if first is None and depth is None:
        return 0
    target_registers = []
    if ps is not None:
        for index in range(len(state.targets)):
            element = next((e for e in ps.outputs if (e["sv"] == 64 or e["name"] == "SV_TARGET") and e["index"] == index), None)
            target_registers.append(element["register"] if element is not None else None)
    ps_bindings = state.bindings.get("ps", empty)

    def fragment(frags):
        if ps is None:
            return [None] * len(targets), None, None
        inputs = {}
        for element in ps.inputs:
            register, sv = element["register"], element["sv"]
            if sv == 1:
                value = numpy.stack([frags.x + 0.5, frags.y + 0.5, frags.z,
                                     1 / numpy.where(frags.inv_w == 0, 1, frags.inv_w)], 1).astype(numpy.float32)
                bits = value.view(numpy.uint32)
            elif sv == 9:
                bits = numpy.repeat(numpy.where(frags.front, 0xFFFFFFFF, 0).astype(numpy.uint32)[:, None], 4, 1)
            elif sv == 7:
                bits = numpy.repeat(primitive_id[numpy.minimum(frags.prim, len(primitive_id) - 1)].astype(numpy.uint32)[:, None], 4, 1) \
                    if len(primitive_id) else numpy.zeros((frags.n, 4), dtype=numpy.uint32)
            elif sv == 10:
                bits = numpy.zeros((frags.n, 4), dtype=numpy.uint32)
            else:
                link = next((l for l in links if l[0] is element), None)
                if link is None:
                    continue
                _, column, integer = link
                values = frags.attrs[:, column:column + 4]
                bits = (numpy.round(values).astype(numpy.int64) & 0xFFFFFFFF).astype(numpy.uint32) if integer \
                    else numpy.ascontiguousarray(values.astype(numpy.float32)).view(numpy.uint32)
            if register in inputs:
                for c in range(4):
                    if element["mask"] >> c & 1:
                        inputs[register][:, c] = bits[:, c]
            else:
                inputs[register] = numpy.array(bits, dtype=numpy.uint32)
        result = dxbc.run_pixel(ps, frags.n, inputs, ps_bindings, helper=frags.helper)
        colors = []
        for register in target_registers:
            value = result.outputs.get(register) if register is not None else None
            colors.append(value.view(numpy.float32) if value is not None else None)
        return colors, result.alive, result.depth

    if fill == 2 and kind == "triangles":
        prims = numpy.concatenate([prims[:, [0, 1]], prims[:, [1, 2]], prims[:, [2, 0]]])
        kind = "lines"
    if ps is not None and kind == "triangles":
        from .gpu import view_levels
        mipmaps = any(view is not None and view.images and len(view_levels(view, 0)) > 1
                      for view in ps_bindings.srvs.values())
        fragment.quads = dxbc.needs_quads(ps, mipmaps)
    # no side effect (no unordered access view): the fragments may be shaded by several threads
    fragment.parallel = ps is None or (not ps.uavs and not ps_bindings.uavs and not getattr(ps, "side_effects", False))
    return draw(output, pipeline, kind, prims, attrs, fragment, clip=clip, flat_mask=flat_mask)


def _tessellate(state, hs, ds, outputs, prims):
    empty = dxbc.Bindings()
    patches = len(prims)
    vicp = {reg: values[prims] for reg, values in outputs.items()}
    vocp, constants = dxbc.run_hull(hs, patches, vicp, state.bindings.get("hs", empty),
                                    numpy.arange(patches, dtype=numpy.uint32))
    domains, owners, tris = [], [], []
    total = 0
    kind = "triangles"
    for patch in range(patches):
        edges, inside = _tess_factors(hs, constants, patch)
        points, prim, kind = dxbc.tessellate(hs.domain, edges or [1.0], inside or [1.0], hs.partitioning,
                                             {1: 1, 2: 2}.get(hs.output_primitive, 3))
        if len(points) == 0:
            continue
        if kind == "triangles" and hs.output_primitive == 4:  # triangle_ccw
            prim = prim[:, [0, 2, 1]]
        domains.append(points)
        owners.append(numpy.full(len(points), patch))
        tris.append(prim + total)
        total += len(points)
    if not domains:
        return {}, numpy.zeros((0, 3), dtype=numpy.int64), kind, ds.outputs, numpy.zeros(0, dtype=numpy.uint32)
    points = numpy.concatenate(domains)
    owner = numpy.concatenate(owners)
    domain = numpy.zeros((len(points), 4), dtype=numpy.float32)
    domain[:, :3] = points
    result = dxbc.run_domain(ds, len(points), domain.view(numpy.uint32), {reg: v[owner] for reg, v in vocp.items()},
                             {reg: v[owner] for reg, v in constants.items()}, state.bindings.get("ds", empty),
                             owner.astype(numpy.uint32))
    prims = numpy.concatenate(tris)
    return result.outputs, prims, kind, ds.outputs, owner[prims[:, 0]].astype(numpy.uint32)


def _geometry(state, gs, outputs, prims, primitive_id):
    return _geometry_streams(state, gs, outputs, prims, primitive_id, [0]).get(
        0, ({}, numpy.zeros((0, 3), dtype=numpy.int64), "triangles", numpy.zeros(0, dtype=numpy.uint32)))


def _geometry_streams(state, gs, outputs, prims, primitive_id, streams):
    """The geometry shader on the primitives: per stream asked, (outputs, primitives, kind, primitive ids), the
    primitives in the order of the input primitives, then of the instances, then of their emission."""
    expected = dxbc.GS_INPUT_VERTICES.get(gs.gs_input, prims.shape[1])
    if prims.shape[1] > expected:
        prims = prims[:, :expected]
    inputs2d = {reg: values[prims] for reg, values in outputs.items()}
    results = dxbc.run_geometry(gs, len(prims), inputs2d, state.bindings.get("gs", dxbc.Bindings()), primitive_id)
    strip_size = {1: 1, 2: 2, 3: 2, 5: 3}.get(gs.gs_output, 3)
    kind = {1: "points", 2: "lines", 3: "triangles"}[strip_size]
    found = {}
    for wanted in streams:
        parts: Dict[int, list] = {}
        made_list = []      # (input primitive, instance, sequence, vertices)
        count = 0
        for instance, result in enumerate(results):
            strips = [[] for _ in range(len(prims))]
            for event, stream, lanes, snapshot in result.events:
                if stream != wanted:
                    continue
                if event == "cut":
                    for lane in lanes:
                        strips[lane] = []
                    continue
                ids = range(count, count + len(lanes))
                count += len(lanes)
                for reg, values in snapshot.items():
                    parts.setdefault(reg, []).append((ids.start, values))
                for lane, vertex in zip(lanes, ids):
                    strip = strips[lane]
                    strip.append(vertex)
                    made = None
                    if strip_size == 1:
                        made = (vertex,)
                    elif strip_size == 2 and len(strip) >= 2:
                        made = (strip[-2], strip[-1])
                    elif strip_size == 3 and len(strip) >= 3:
                        made = (strip[-3], strip[-2], strip[-1]) if (len(strip) - 3) % 2 == 0 else (strip[-2], strip[-3], strip[-1])
                    if made is not None:
                        made_list.append((int(lane), instance, len(made_list), made))
        if count == 0 or not made_list:
            continue
        made_list.sort(key=lambda item: item[:3])
        merged = {}
        for reg, pieces in parts.items():
            array = numpy.zeros((count, 4), dtype=numpy.uint32)
            for start, values in pieces:
                array[start:start + len(values)] = values
            merged[reg] = array
        out_prims = numpy.array([item[3] for item in made_list], dtype=numpy.int64).reshape(-1, strip_size)
        out_ids = numpy.array([int(primitive_id[item[0]]) for item in made_list], dtype=numpy.uint32)
        found[wanted] = (merged, out_prims, kind, out_ids)
    return found


def dispatch(shader, groups, bindings) -> None:
    dxbc.run_compute(shader, groups, bindings)
