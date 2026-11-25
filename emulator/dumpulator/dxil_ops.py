# Fathom addition to dumpulator: the DXIL operations of dxil.py beyond the common ones: the geometry, hull
# and domain stages (emit / cut, control points, patch constants, domain location), integer and bit
# operations (64-bit products, carries, msad, bfi, packed dot products, pack / unpack), conversions (16 and
# 64-bit bitcasts, doubles), gathers, comparison sampling with a level / gradients / bias, sample positions
# and attribute evaluation, wave and quad operations, counters, dynamic resources (ResourceDescriptorHeap,
# SamplerDescriptorHeap), sampler feedback, the vectors of shader model 6.9, and system values. Boost
# Software License 1.0, like the rest of dumpulator (LICENSE.dumpulator).

import numpy

from .dxil import (_DX, Handle, Memory, Pointer, _dtype, _from_bits, _bits_of, _is_float, _mask, _res_ret, _sample,
                   _signature_read, _signed, _wave_groups, _width, _dimensions, _coords)


def _scalar(value):
    return int(numpy.asarray(value).ravel()[0]) if isinstance(value, numpy.ndarray) else int(value or 0)


def _float_of(t):
    return _is_float(t)


# ------------------------------------------------------------------ the stages
def _emit(cut, emit):
    def run(m, args, ins, lanes, active):
        stream = _scalar(args[0]) if args else 0
        alive = numpy.nonzero(active)[0]
        if emit:
            snapshot = {k: v[alive].copy() for k, v in m.result.outputs.items()}
            m.result.events.append(("emit", stream, alive, snapshot))
        if cut:
            m.result.events.append(("cut", stream, alive, None))
        return None
    return run


def _store_patch_constant(m, args, ins, lanes, active):
    ident, row, column = _scalar(args[0]), _scalar(args[1]), _scalar(args[2])
    register, first, _, _ = m.s.patch_elements.get(ident, (ident, 0, "", 0))
    target = getattr(m, "patch_outputs", None)
    if target is None:
        target = m.patch_outputs = {}
    out = target.setdefault(register + row, numpy.zeros((m.n, 4), dtype=numpy.uint32))
    t = m.f.all_values[ins.operands[4]].type
    component = min(first + column, 3)
    out[:, component] = numpy.where(active, _bits_of(args[3], t), out[:, component])
    return None


def _load_output_control_point(m, args, ins, lanes, active):
    return _signature_read(m, m.s.output_elements, m.vocp, {}, args, ins)


def _load_patch_constant(m, args, ins, lanes, active):
    return _signature_read(m, m.s.patch_elements, {}, m.vpc, list(args[:3]), ins)


def _domain_location(m, args, ins, lanes, active):
    domain = m.system.get("domain")
    if domain is None:
        return numpy.zeros(m.n, dtype=numpy.float32)
    domain = numpy.asarray(domain).reshape(m.n, -1)
    c = _scalar(args[0])
    return domain[:, c].astype(numpy.float32) if c < domain.shape[1] else numpy.zeros(m.n, dtype=numpy.float32)


def _system_value(name, default=0):
    def run(m, args, ins, lanes, active):
        value = m.system.get(name)
        if value is None:
            return numpy.full(m.n, default, dtype=_dtype(ins.type))
        value = numpy.broadcast_to(numpy.asarray(value), (m.n,) + numpy.asarray(value).shape[1:])
        if value.ndim > 1:
            value = value[:, _scalar(args[0]) if args else 0]
        return value.astype(_dtype(ins.type))
    return run


# ------------------------------------------------------------------ integers and bits
def _two(a, b):
    return (numpy.asarray(a, dtype=numpy.int64), numpy.asarray(b, dtype=numpy.int64))


def _imul(signed):
    def run(m, args, ins, lanes, active):
        a, b = _two(*args[:2])
        if signed:
            product = _signed(a, 32).astype(object) * _signed(b, 32).astype(object)
        else:
            product = (a & 0xFFFFFFFF).astype(object) * (b & 0xFFFFFFFF).astype(object)
        lo = numpy.array([int(p) & 0xFFFFFFFF for p in product], dtype=numpy.int64)
        hi = numpy.array([(int(p) >> 32) & 0xFFFFFFFF for p in product], dtype=numpy.int64)
        return (hi, lo)
    return run


def _udiv(m, args, ins, lanes, active):
    a, b = (x & 0xFFFFFFFF for x in _two(*args[:2]))
    zero = b == 0
    safe = numpy.where(zero, 1, b)
    return (numpy.where(zero, 0xFFFFFFFF, a // safe), numpy.where(zero, 0xFFFFFFFF, a % safe))


def _uaddc(m, args, ins, lanes, active):
    a, b = (x & 0xFFFFFFFF for x in _two(*args[:2]))
    total = a + b
    return (total & 0xFFFFFFFF, (total >> 32).astype(numpy.int64))


def _usubb(m, args, ins, lanes, active):
    a, b = (x & 0xFFFFFFFF for x in _two(*args[:2]))
    return ((a - b) & 0xFFFFFFFF, (a < b).astype(numpy.int64))


def _msad(m, args, ins, lanes, active):
    reference, source, accumulator = (numpy.asarray(x, dtype=numpy.int64) & 0xFFFFFFFF for x in args[:3])
    out = accumulator.copy()
    for k in range(4):
        r, s = (reference >> (8 * k)) & 0xFF, (source >> (8 * k)) & 0xFF
        out += numpy.where(r != 0, numpy.abs(r - s), 0)
    return out & 0xFFFFFFFF


def _bfi(m, args, ins, lanes, active):
    width, offset, value, base = (numpy.asarray(x, dtype=numpy.int64) & 0xFFFFFFFF for x in args[:4])
    width, offset = width & 31, offset & 31
    bits = ((1 << width) - 1) << offset
    return (((value << offset) & bits) | (base & ~bits)) & 0xFFFFFFFF


def _dot4add(signed):
    def run(m, args, ins, lanes, active):
        accumulator, a, b = (numpy.asarray(x, dtype=numpy.int64) & 0xFFFFFFFF for x in args[:3])
        total = _signed(accumulator, 32) if signed else accumulator
        for k in range(4):
            x, y = (a >> (8 * k)) & 0xFF, (b >> (8 * k)) & 0xFF
            if signed:
                x, y = _signed(x, 8), _signed(y, 8)
            total = total + x * y
        return total & 0xFFFFFFFF
    return run


def _unpack4x8(m, args, ins, lanes, active):
    mode, packed = _scalar(args[0]), numpy.asarray(args[1], dtype=numpy.int64) & 0xFFFFFFFF
    bits = _width(ins.type.elements[0])
    out = []
    for k in range(4):
        byte = (packed >> (8 * k)) & 0xFF
        out.append(_mask(_signed(byte, 8), bits) if mode == 1 else byte)
    return tuple(out)


def _pack4x8(m, args, ins, lanes, active):
    mode = _scalar(args[0])
    t = m.f.all_values[ins.operands[2]].type
    out = numpy.zeros(m.n, dtype=numpy.int64)
    for k, value in enumerate(args[1:5]):
        v = _signed(numpy.asarray(value, dtype=numpy.int64), _width(t))
        if mode == 1:
            v = numpy.clip(v, 0, 255)
        elif mode == 2:
            v = numpy.clip(v, -128, 127)
        out |= (v & 0xFF) << (8 * k)
    return out


def _is_normal(m, args, ins, lanes, active):
    x = numpy.asarray(args[0], dtype=numpy.float64)
    t = m.f.all_values[ins.operands[1]].type
    tiny = {16: 6.103515625e-05, 32: 1.1754943508222875e-38, 64: 2.2250738585072014e-308}.get(t.bits, 1.1754943508222875e-38)
    return (numpy.isfinite(x) & (numpy.abs(x) >= tiny)).astype(numpy.int64)


# ------------------------------------------------------------------ conversions
def _bitcast(source_bits, float_out):
    def run(m, args, ins, lanes, active):
        value = numpy.asarray(args[0])
        if float_out:       # integer bits -> float
            kind = {16: (numpy.uint16, numpy.float16), 32: (numpy.uint32, numpy.float32), 64: (numpy.uint64, numpy.float64)}[source_bits]
            out = (value.astype(numpy.int64) & ((1 << source_bits) - 1) if source_bits < 64 else value.astype(numpy.int64)) \
                .astype(kind[0]).view(kind[1])
            return out.astype(numpy.float32) if source_bits == 16 else out
        kind = {16: (numpy.float16, numpy.uint16), 32: (numpy.float32, numpy.uint32), 64: (numpy.float64, numpy.uint64)}[source_bits]
        return value.astype(kind[0]).view(kind[1]).astype(numpy.int64)
    return run


def _make_double(m, args, ins, lanes, active):
    lo, hi = (numpy.asarray(x, dtype=numpy.int64) & 0xFFFFFFFF for x in args[:2])
    return ((hi << 32) | lo).astype(numpy.uint64).view(numpy.float64)


def _split_double(m, args, ins, lanes, active):
    bits = numpy.asarray(args[0], dtype=numpy.float64).view(numpy.uint64).astype(object)
    lo = numpy.array([int(b) & 0xFFFFFFFF for b in bits], dtype=numpy.int64)
    hi = numpy.array([int(b) >> 32 for b in bits], dtype=numpy.int64)
    return (lo, hi)


def _legacy_double(kind):
    def run(m, args, ins, lanes, active):
        value = numpy.nan_to_num(numpy.asarray(args[0], dtype=numpy.float64))
        if kind == "float":
            return value.astype(numpy.float32)
        if kind == "sint":
            return numpy.clip(numpy.trunc(value), -2 ** 31, 2 ** 31 - 1).astype(numpy.int64) & 0xFFFFFFFF
        return numpy.clip(numpy.trunc(value), 0, 2 ** 32 - 1).astype(numpy.int64)
    return run


# ------------------------------------------------------------------ resources
def _cbuffer_load(m, args, ins, lanes, active):
    handle, offset = args[0], numpy.asarray(args[1], dtype=numpy.int64)
    data = m._cb(handle)
    if data is None:
        return numpy.zeros(m.n, dtype=_dtype(ins.type))
    words = numpy.asarray(data, dtype=numpy.uint32).reshape(-1)
    index = numpy.clip(offset // 4, 0, len(words) - 1)
    bits = words[index].astype(numpy.int64)
    if ins.type.bits == 64:
        bits |= words[numpy.clip(index + 1, 0, len(words) - 1)].astype(numpy.int64) << 32
    return _from_bits(bits, ins.type)


def _update_counter(m, args, ins, lanes, active):
    view = m._view(args[0])
    out = numpy.zeros(m.n, dtype=numpy.int64)
    if view is None:
        return out
    step = 1 if _signed(numpy.asarray(args[1], dtype=numpy.int64), 8).ravel()[0] >= 0 else -1
    if view.counter is None:
        view.counter = [0]
    counter = view.counter
    for lane in numpy.nonzero(active & ~m.helper)[0]:
        if step > 0:
            out[lane] = counter[0]
            counter[0] += 1
        else:
            counter[0] -= 1
            out[lane] = counter[0]
        counter[0] &= 0xFFFFFFFF
    view.dirty = True
    return out & 0xFFFFFFFF


def _gather(compare, raw=False):
    def run(m, args, ins, lanes, active):
        view = m._view(args[0])
        out = numpy.zeros((m.n, 4))
        image = view.level(0, 0) if view is not None else None
        if image is None:
            return _res_ret(m, out, ins.type)
        coords = _coords(args[2:6], _dimensions(view))
        dx = _scalar(args[6]) if isinstance(args[6], numpy.ndarray) else 0
        dy = _scalar(args[7]) if isinstance(args[7], numpy.ndarray) else 0
        channel = 0 if raw else _scalar(args[8])
        texels = image.raw_texels() if raw and hasattr(image, "raw_texels") else image.texels()
        x = numpy.floor(coords[:, 0] * image.width - 0.5).astype(numpy.int64) + dx
        y = numpy.floor(coords[:, 1] * image.height - 0.5).astype(numpy.int64) + dy
        for k, (ox, oy) in enumerate(((0, 1), (1, 1), (1, 0), (0, 0))):
            xs = numpy.clip(x + ox, 0, image.width - 1)
            ys = numpy.clip(y + oy, 0, image.height - 1)
            out[:, k] = texels[ys, xs, channel]
        if compare:
            reference = numpy.asarray(args[9], dtype=numpy.float64)
            out = (reference[:, None] <= out).astype(numpy.float64)
        return _res_ret(m, out, ins.type)
    return run


def _sample_extra(kind):
    """SampleCmpLevel, SampleCmpGrad, SampleCmpBias (compare + level / gradients / bias)."""
    def run(m, args, ins, lanes, active):
        from .gpu import DEFAULT_SAMPLER, sample_view
        view, sampler_handle = m._view(args[0]), args[1]
        sampler = m._sampler(sampler_handle)
        if view is None:
            return _res_ret(m, numpy.zeros((m.n, 4)), ins.type)
        count = _dimensions(view)
        coords = _coords(args[2:6], count)
        offsets = [_scalar(o) if isinstance(o, numpy.ndarray) else 0 for o in args[6:8]]
        rest = args[9:]
        compare = numpy.asarray(rest[0], dtype=numpy.float64)
        lod = bias = gradients = None
        if kind == "level":
            lod = numpy.asarray(rest[1], dtype=numpy.float64)
        elif kind == "grad":
            gradients = (_coords(rest[1:4], count), _coords(rest[4:7], count))
        else:
            bias = numpy.asarray(rest[1], dtype=numpy.float64)
        values = sample_view(view, sampler or DEFAULT_SAMPLER, coords, lod, compare, tuple(offsets[:2]), bias, gradients)
        return _res_ret(m, values, ins.type)
    return run


def _texture_store_sample(m, args, ins, lanes, active):
    from .gpu import store_view
    view = m._view(args[0])
    if view is None:
        return None
    coords = numpy.stack([numpy.asarray(a, dtype=numpy.int64) for a in args[1:4]], 1)
    values = numpy.stack([numpy.asarray(a, dtype=numpy.float64) for a in args[4:8]], 1)
    store_view(view, coords, values, numpy.nonzero(active & ~m.helper)[0])     # one sample per pixel
    view.dirty = True
    return None


# the standard sample positions of Direct3D (1, 2, 4, 8 and 16 samples), in 1/16 of a pixel
_POSITIONS = {1: [(0, 0)], 2: [(4, 4), (-4, -4)], 4: [(-2, -6), (6, -2), (-6, 2), (2, 6)],
              8: [(1, -3), (-1, 3), (5, 1), (-3, -5), (-5, 5), (-7, -1), (3, 7), (7, -7)],
              16: [(1, 1), (-1, -3), (-3, 2), (4, -1), (-5, -2), (2, 5), (5, 3), (3, -5), (-2, 6), (0, -7), (-4, -6),
                   (-6, 4), (-8, 0), (7, -4), (6, 7), (-7, -8)]}


def _sample_position(m, args, ins, lanes, active, count=1, index=None):
    index = numpy.asarray(index if index is not None else 0, dtype=numpy.int64)
    table = numpy.array(_POSITIONS.get(count, [(0, 0)]), dtype=numpy.float32) / 16
    pick = table[numpy.clip(numpy.broadcast_to(index, (m.n,)), 0, len(table) - 1)]
    return (pick[:, 0].copy(), pick[:, 1].copy())


def _texture_sample_position(m, args, ins, lanes, active):
    view = m._view(args[0])
    count = getattr(view, "samples", 1) if view is not None else 1
    return _sample_position(m, args, ins, lanes, active, count, args[1])


def _eval(m, args, ins, lanes, active):
    """EvalSnapped / EvalSampleIndex / EvalCentroid: the attribute at the pixel (one sample per pixel)."""
    return _signature_read(m, m.s.input_elements, {}, m.inputs, list(args[:3]), ins)


def _create_handle_from_heap(m, args, ins, lanes, active):
    """ResourceDescriptorHeap[i] / SamplerDescriptorHeap[i] (shader model 6.6): a handle on the heap's slot."""
    index = _scalar(args[0])
    sampler = bool(_scalar(args[1]))
    return Handle(3 if sampler else 0, ("heap", sampler, index))


# ------------------------------------------------------------------ waves and quads
def _per_wave(m, active, function):
    wave, index = _wave_groups(m)
    for w in numpy.unique(wave[active]):
        function(numpy.nonzero(active & (wave == w))[0], numpy.nonzero(wave == w)[0], index)


def _wave_all_equal(m, args, ins, lanes, active):
    value = numpy.asarray(args[0])
    out = numpy.zeros(m.n, dtype=numpy.int64)

    def each(sel, whole, index):
        out[whole] = int(numpy.all(value[sel] == value[sel[0]]))
    _per_wave(m, active, each)
    return out


def _wave_read_lane_at(m, args, ins, lanes, active):
    value, lane = numpy.asarray(args[0]), numpy.asarray(args[1], dtype=numpy.int64)
    out = value.copy()

    def each(sel, whole, index):
        base = whole[0]
        source = numpy.clip(base + lane[whole], base, whole[-1])
        out[whole] = value[source]
    _per_wave(m, active, each)
    return out


def _wave_active_bit(m, args, ins, lanes, active):
    value, op = numpy.asarray(args[0], dtype=numpy.int64), _scalar(args[1])
    out = numpy.zeros(m.n, dtype=numpy.int64)

    def each(sel, whole, index):
        items = value[sel]
        result = numpy.bitwise_and.reduce(items) if op == 0 else numpy.bitwise_or.reduce(items) if op == 1 \
            else numpy.bitwise_xor.reduce(items)
        out[whole] = result
    _per_wave(m, active, each)
    return out


def _wave_all_bit_count(m, args, ins, lanes, active):
    value = numpy.asarray(args[0]).astype(bool)
    out = numpy.zeros(m.n, dtype=numpy.int64)

    def each(sel, whole, index):
        out[whole] = int(value[sel].sum())
    _per_wave(m, active, each)
    return out


def _wave_prefix_bit_count(m, args, ins, lanes, active):
    value = numpy.asarray(args[0]).astype(bool)
    out = numpy.zeros(m.n, dtype=numpy.int64)

    def each(sel, whole, index):
        running = 0
        for lane in sel:
            out[lane] = running
            running += int(value[lane])
    _per_wave(m, active, each)
    return out


def _wave_multi_prefix(bit_count):
    def run(m, args, ins, lanes, active):
        value = numpy.asarray(args[0])
        masks = [numpy.asarray(x, dtype=numpy.int64) & 0xFFFFFFFF for x in args[1:5]]
        op = _scalar(args[5]) if not bit_count else 0
        signed = _scalar(args[6]) if not bit_count and len(args) > 6 else 0
        out = numpy.zeros(m.n, dtype=numpy.int64 if bit_count else _dtype(ins.type))
        wave, index = _wave_groups(m)
        for lane in numpy.nonzero(active)[0]:
            mask = int(masks[0][lane]) | (int(masks[1][lane]) << 32)
            peers = [p for p in numpy.nonzero(active & (wave == wave[lane]))[0] if p < lane and mask >> int(index[p]) & 1]
            if bit_count:
                out[lane] = sum(int(bool(value[p])) for p in peers)
                continue
            items = value[peers]
            if op == 0:         # sum
                out[lane] = items.sum() if len(items) else 0
            elif op == 1:       # and
                out[lane] = numpy.bitwise_and.reduce(items.astype(numpy.int64)) if len(items) else -1
            elif op == 2:       # or
                out[lane] = numpy.bitwise_or.reduce(items.astype(numpy.int64)) if len(items) else 0
            elif op == 3:       # xor
                out[lane] = numpy.bitwise_xor.reduce(items.astype(numpy.int64)) if len(items) else 0
            else:               # product
                out[lane] = items.prod() if len(items) else 1
        del signed
        return out if bit_count or _is_float(ins.type) else out & ((1 << _width(ins.type)) - 1)
    return run


def _quad(m, args, ins, lanes, active, mapping):
    value = numpy.asarray(args[0])
    lane = numpy.arange(m.n)
    source = (lane & ~3) | mapping(lane & 3)
    return value[numpy.clip(source, 0, m.n - 1)]


def _quad_read_lane_at(m, args, ins, lanes, active):
    target = numpy.asarray(args[1], dtype=numpy.int64) & 3
    return _quad(m, args, ins, lanes, active, lambda q: numpy.broadcast_to(target, q.shape))


def _quad_op(m, args, ins, lanes, active):
    op = _scalar(args[1])
    return _quad(m, args, ins, lanes, active, lambda q: q ^ (1 if op == 0 else 2 if op == 1 else 3))


def _wave_match(m, args, ins, lanes, active):
    value = numpy.asarray(args[0])
    words = [numpy.zeros(m.n, dtype=numpy.int64) for _ in range(4)]
    wave, index = _wave_groups(m)
    for lane in numpy.nonzero(active)[0]:
        peers = numpy.nonzero(active & (wave == wave[lane]) & (value == value[lane]))[0]
        mask = 0
        for p in peers:
            mask |= 1 << int(index[p])
        for k in range(4):
            words[k][lane] = (mask >> (32 * k)) & 0xFFFFFFFF
    return tuple(words)


def _quad_vote(m, args, ins, lanes, active):
    value = numpy.asarray(args[0]).astype(bool) & active
    quads = value.reshape(-1, 4) if m.n % 4 == 0 else numpy.pad(value, (0, -m.n % 4)).reshape(-1, 4)
    result = quads.all(axis=1) if _scalar(args[1]) == 1 else quads.any(axis=1)
    return numpy.repeat(result, 4)[:m.n].astype(numpy.int64)


def _group_wave_index(m, args, ins, lanes, active):
    flat = m.system.get("flattened")
    lane = numpy.asarray(flat, dtype=numpy.int64) if flat is not None else numpy.arange(m.n)
    from .dxil import WAVE
    return lane // WAVE


def _group_wave_count(m, args, ins, lanes, active):
    from .dxil import WAVE
    size = 1
    for v in m.s.thread_group:
        size *= max(1, v)
    return numpy.full(m.n, -(-size // WAVE), dtype=numpy.int64)


def _dot2_add_half(m, args, ins, lanes, active):
    accumulator = numpy.asarray(args[0], dtype=numpy.float32)
    ax, ay, bx, by = (numpy.asarray(v, dtype=numpy.float16).astype(numpy.float32) for v in args[1:5])
    return (accumulator + ax * bx + ay * by).astype(numpy.float32)


def _attribute_at_vertex(m, args, ins, lanes, active):
    """GetAttributeAtVertex (SV_Barycentrics): the attribute of a vertex of the primitive, when the
    rasterizer gave them (system "vertex_inputs": register -> (n, 3, 4)), else the interpolated one."""
    per_vertex = m.system.get("vertex_inputs") or {}
    return _signature_read(m, m.s.input_elements, per_vertex, m.inputs, args, ins)


# ------------------------------------------------------------------ mesh and amplification shaders
class MeshOutput:
    """What the groups of a mesh shader output: counts, vertex and primitive attributes, indices."""

    def __init__(self, groups, max_vertices, max_primitives):
        self.groups, self.max_vertices, self.max_primitives = groups, max(1, max_vertices), max(1, max_primitives)
        self.counts = numpy.zeros((groups, 2), dtype=numpy.int64)
        self.indices = numpy.zeros((groups * self.max_primitives, 3), dtype=numpy.int64)
        self.vertices = {}          # register -> (groups * max vertices, 4) uint32
        self.primitives = {}        # register -> (groups * max primitives, 4) uint32


def _group_of(m):
    return numpy.asarray(m.groups, dtype=numpy.int64)


def _set_mesh_counts(m, args, ins, lanes, active):
    out = getattr(m, "mesh_out", None)
    if out is not None:
        group = _group_of(m)
        v, p = numpy.asarray(args[0], dtype=numpy.int64), numpy.asarray(args[1], dtype=numpy.int64)
        for lane in numpy.nonzero(active)[0]:
            out.counts[group[lane]] = (min(int(v[lane]), out.max_vertices), min(int(p[lane]), out.max_primitives))
    return None


def _emit_indices(m, args, ins, lanes, active):
    out = getattr(m, "mesh_out", None)
    if out is not None:
        group = _group_of(m)
        prim = numpy.asarray(args[0], dtype=numpy.int64)
        ok = active & (prim >= 0) & (prim < out.max_primitives)
        row = group * out.max_primitives + numpy.clip(prim, 0, out.max_primitives - 1)
        for k, value in enumerate(args[1:4]):
            if isinstance(value, numpy.ndarray):
                out.indices[row[ok], k] = numpy.asarray(value, dtype=numpy.int64)[ok]
    return None


def _store_mesh(primitive):
    def run(m, args, ins, lanes, active):
        out = getattr(m, "mesh_out", None)
        if out is None:
            return None
        ident, row, column = _scalar(args[0]), _scalar(args[1]), _scalar(args[2])
        elements = m.s.patch_elements if primitive else m.s.output_elements
        register, first, _, _ = elements.get(ident, (ident, 0, "", 0))
        limit = out.max_primitives if primitive else out.max_vertices
        table = out.primitives if primitive else out.vertices
        target = table.setdefault(register + row, numpy.zeros((out.groups * limit, 4), dtype=numpy.uint32))
        index = numpy.asarray(args[4], dtype=numpy.int64)
        ok = active & (index >= 0) & (index < limit)
        place = _group_of(m) * limit + numpy.clip(index, 0, limit - 1)
        t = m.f.all_values[ins.operands[4]].type
        target[place[ok], min(first + column, 3)] = _bits_of(args[3], t)[ok]
        return None
    return run


def _get_mesh_payload(m, args, ins, lanes, active):
    payload = m.system.get("mesh_payload")
    if payload is not None:
        return payload
    return Pointer(Memory(ins.type.pointee, m.n), numpy.zeros(m.n, dtype=numpy.int64), numpy.arange(m.n))


def _dispatch_mesh(m, args, ins, lanes, active):
    """DispatchMesh of an amplification shader: the grid of mesh groups and the payload of each of its groups."""
    record = getattr(m, "amplification", None)
    if record is None:
        return None
    group = _group_of(m)
    pointer = args[3]
    for lane in numpy.nonzero(active)[0]:
        g = int(group[lane])
        if g in record:
            continue
        counts = tuple(int(numpy.asarray(a)[lane]) for a in args[:3])
        cells = []
        if isinstance(pointer, Pointer):
            first = int(numpy.asarray(pointer.offset).ravel()[lane])
            row = int(numpy.broadcast_to(pointer.rows, (m.n,))[lane])
            cells = [(pointer.memory.types[k], pointer.memory.cells[k][row]) for k in range(first, len(pointer.memory.cells))]
        record[g] = (counts, cells)
    return None


def _groups(shader, groups, run_group_batch):
    """Thread groups (x, y, z) in batches, as dxil.run_compute: run_group_batch(ids, n, system, group of each lane, count)."""
    tx, ty, tz = (max(1, v) for v in shader.thread_group)
    per_group = tx * ty * tz
    gx, gy, gz = (max(0, int(v)) for v in groups)
    total = gx * gy * gz
    if total == 0:
        return
    batch = max(1, (1 << 18) // per_group)
    local = numpy.arange(per_group)
    lx, ly, lz = local % tx, (local // tx) % ty, local // (tx * ty)
    for first in range(0, total, batch):
        ids = numpy.arange(first, min(total, first + batch))
        count = len(ids)
        group = numpy.repeat(ids, per_group)
        g = numpy.stack([group % gx, (group // gx) % gy, group // (gx * gy)], 1)
        t = numpy.stack([numpy.tile(lx, count), numpy.tile(ly, count), numpy.tile(lz, count)], 1)
        system = {"thread_id": g * [tx, ty, tz] + t, "group_id": g, "thread_in_group": t, "flattened": numpy.tile(local, count)}
        run_group_batch(ids, count * per_group, system, numpy.repeat(numpy.arange(count), per_group), count)


def run_amplification(shader, groups, bindings):
    """An amplification shader on its groups: [(mesh groups (x, y, z), payload [(type, value)])] in group order."""
    from .dxil import Machine
    shader.check()
    out = []

    def batch(ids, n, system, lane_group, count):
        machine = Machine(shader, shader.function, n, bindings, system=system, groups=lane_group, group_count=count)
        machine.amplification = {}
        machine.run()
        for k in range(count):
            out.append(machine.amplification.get(k, ((0, 0, 0), [])))
    _groups(shader, groups, batch)
    return out


def run_mesh(shader, groups, bindings, payload=None):
    """A mesh shader on its groups: (vertex outputs reg -> (v, 4), primitive outputs reg -> (p, 4), indices (p, 3))."""
    from .dxil import Machine
    shader.check()
    max_vertices, max_primitives = (shader.mesh or (3, 1, 2, 0))[:2]
    vertices, primitives, indices = {}, {}, []
    total = [0]

    def batch(ids, n, system, lane_group, count):
        if payload:
            memory = Memory.__new__(Memory)
            memory.types = [t for t, _ in payload]
            memory.cells = [numpy.full(n, v, dtype=numpy.asarray(v).dtype) for _, v in payload]
            system["mesh_payload"] = Pointer(memory, numpy.zeros(n, dtype=numpy.int64), numpy.arange(n))
        machine = Machine(shader, shader.function, n, bindings, system=system, groups=lane_group, group_count=count)
        machine.mesh_out = out = MeshOutput(count, max_vertices, max_primitives)
        machine.run()
        for k in range(count):
            v, p = (int(x) for x in out.counts[k])
            base_v, base_p = k * out.max_vertices, k * out.max_primitives
            for reg, values in out.vertices.items():
                vertices.setdefault(reg, []).append(values[base_v:base_v + v])
            for reg, values in out.primitives.items():
                primitives.setdefault(reg, []).append(values[base_p:base_p + p])
            indices.append(numpy.clip(out.indices[base_p:base_p + p], 0, max(v - 1, 0)) + total[0])
            total[0] += v
    _groups(shader, groups, batch)
    vertex_out = {reg: numpy.concatenate(parts) for reg, parts in vertices.items()}
    primitive_out = {reg: numpy.concatenate(parts) for reg, parts in primitives.items()}
    prims = numpy.concatenate(indices) if indices else numpy.zeros((0, 3), dtype=numpy.int64)
    return vertex_out, primitive_out, prims


# ------------------------------------------------------------------ vectors of shader model 6.9
def _vector_load(m, args, ins, lanes, active):
    data, view = m._bytes(args[0])
    t = ins.type.elements[0]
    count = t.count if t.kind == "vector" else 1
    base = t.elements[0] if t.kind == "vector" else t
    size = max(1, base.bits // 8)
    out = [numpy.zeros(m.n, dtype=_dtype(base)) for _ in range(count)]
    if data is None:
        return (tuple(out), numpy.zeros(m.n, dtype=numpy.int64))
    from .dxil import _address
    address = _address(view, args[0], args[1], args[2], m.n)
    raw = numpy.frombuffer(bytes(data), dtype=numpy.uint8)
    kind = {1: numpy.uint8, 2: numpy.uint16, 4: numpy.uint32, 8: numpy.uint64}[size]
    for k in range(count):
        place = address + k * size
        ok = (place >= 0) & (place + size <= len(raw))
        words = numpy.zeros(m.n, dtype=numpy.int64)
        for lane in numpy.nonzero(ok)[0]:
            words[lane] = int(numpy.frombuffer(raw[place[lane]:place[lane] + size].tobytes(), dtype=kind)[0])
        out[k] = _from_bits(words, base)
    return (tuple(out), numpy.zeros(m.n, dtype=numpy.int64))


def _vector_store(m, args, ins, lanes, active):
    data, view = m._bytes(args[0])
    if data is None:
        return None
    from .dxil import _address
    vector = args[3]
    t = m.f.all_values[ins.operands[4]].type
    base = t.elements[0] if t.kind == "vector" else t
    items = vector if isinstance(vector, tuple) else (vector,)
    size = max(1, base.bits // 8)
    address = _address(view, args[0], args[1], args[2], m.n)
    raw = data.view(numpy.uint8)
    for lane in numpy.nonzero(active & ~m.helper)[0]:
        for k, item in enumerate(items):
            place = int(address[lane]) + k * size
            if 0 <= place and place + size <= len(raw):
                bits = int(_bits_of(numpy.asarray(item)[lane:lane + 1], base)[0])
                raw[place:place + size] = numpy.frombuffer(bits.to_bytes(size, "little"), dtype=numpy.uint8)
    view.dirty = True
    return None


def _vector_reduce(op):
    def run(m, args, ins, lanes, active):
        items = args[0] if isinstance(args[0], tuple) else (args[0],)
        out = numpy.asarray(items[0], dtype=numpy.int64).copy()
        for item in items[1:]:
            out = out & numpy.asarray(item, dtype=numpy.int64) if op == "and" else out | numpy.asarray(item, dtype=numpy.int64)
        return out
    return run


def _fdot(m, args, ins, lanes, active):
    a = args[0] if isinstance(args[0], tuple) else (args[0],)
    b = args[1] if isinstance(args[1], tuple) else (args[1],)
    total = numpy.zeros(m.n, dtype=numpy.float64)
    for x, y in zip(a, b):
        total += numpy.asarray(x, dtype=numpy.float64) * numpy.asarray(y, dtype=numpy.float64)
    return total.astype(_dtype(ins.type))


# ------------------------------------------------------------------ the rest
def _temp_load(m, args, ins, lanes, active):
    temps = m.__dict__.setdefault("temp_registers", {})
    return _from_bits(temps.get(_scalar(args[0]), numpy.zeros(m.n, dtype=numpy.uint32)), ins.type)


def _temp_store(m, args, ins, lanes, active):
    temps = m.__dict__.setdefault("temp_registers", {})
    t = m.f.all_values[ins.operands[2]].type
    old = temps.get(_scalar(args[0]), numpy.zeros(m.n, dtype=numpy.uint32))
    temps[_scalar(args[0])] = numpy.where(active, _bits_of(args[1], t), old)
    return None


def _constant(value):
    return lambda m, args, ins, lanes, active: numpy.full(m.n, value, dtype=_dtype(ins.type))


def _nothing(m, args, ins, lanes, active):
    return None


def _feedback(m, args, ins, lanes, active):
    """WriteSamplerFeedback*: the feedback map records the mip level 0 as used (MinMip: 0 where sampled)."""
    view = m._view(args[0])
    if view is not None:
        view.dirty = True
    return None


def _cycle_counter(m, args, ins, lanes, active):
    clock = m.__dict__.get("cycles", 0) + 1
    m.cycles = clock
    return (numpy.full(m.n, clock & 0xFFFFFFFF, dtype=numpy.int64), numpy.zeros(m.n, dtype=numpy.int64))


_DX.update({
    # stages
    97: _emit(False, True), 98: _emit(True, False), 99: _emit(True, True), 100: _system_value("gs_instance"),
    103: _load_output_control_point, 104: _load_patch_constant, 105: _domain_location, 106: _store_patch_constant,
    107: _system_value("output_cp_id"), 138: _system_value("view_id"), 90: _system_value("sample_index"),
    91: _constant(1), 92: _constant(1), 256: _system_value("start_vertex"), 257: _system_value("start_instance"),
    # integers and bits
    41: _imul(True), 42: _imul(False), 43: _udiv, 44: _uaddc, 45: _usubb, 50: _msad, 53: _bfi,
    163: _dot4add(True), 164: _dot4add(False), 219: _unpack4x8, 220: _pack4x8, 11: _is_normal,
    # conversions
    124: _bitcast(16, True), 125: _bitcast(16, False), 126: _bitcast(32, True), 127: _bitcast(32, False),
    128: _bitcast(64, True), 129: _bitcast(64, False), 101: _make_double, 102: _split_double,
    132: _legacy_double("float"), 133: _legacy_double("sint"), 134: _legacy_double("uint"),
    # resources
    58: _cbuffer_load, 70: _update_counter, 71: _constant(1), 73: _gather(False), 74: _gather(True),
    223: _gather(False, raw=True), 224: _sample_extra("level"), 254: _sample_extra("grad"), 255: _sample_extra("bias"),
    225: _texture_store_sample, 75: _texture_sample_position,
    76: lambda m, a, i, l, act: _sample_position(m, a, i, l, act, 1, a[0]), 77: _constant(1),
    87: _eval, 88: _eval, 89: _eval, 218: _create_handle_from_heap,
    174: _feedback, 175: _feedback, 176: _feedback, 177: _feedback,
    # waves and quads
    115: _wave_all_equal, 117: _wave_read_lane_at, 120: _wave_active_bit, 122: _quad_read_lane_at, 123: _quad_op,
    135: _wave_all_bit_count, 136: _wave_prefix_bit_count, 166: _wave_multi_prefix(False), 167: _wave_multi_prefix(True),
    165: _wave_match, 222: _quad_vote, 312: _group_wave_index, 313: _group_wave_count, 162: _dot2_add_half,
    137: _attribute_at_vertex,
    # mesh and amplification shaders
    168: _set_mesh_counts, 169: _emit_indices, 170: _get_mesh_payload, 171: _store_mesh(False), 172: _store_mesh(True),
    173: _dispatch_mesh,
    # shader model 6.9 vectors
    303: _vector_load, 304: _vector_store, 309: _vector_reduce("and"), 310: _vector_reduce("or"), 311: _fdot,
    # the rest
    0: _temp_load, 1: _temp_store, 2: _temp_load, 3: _temp_store, 109: _cycle_counter,
    343: _nothing, 344: _constant(0),
})

__all__ = ["Memory", "Pointer", "_float_of", "_sample"]
