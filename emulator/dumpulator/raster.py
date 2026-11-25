# Fathom addition to dumpulator: the software rasterizer of the 3D APIs the emulator implements (OpenGL
# in opengl_emu.py / gl_emu.py, Direct3D 9 / 10 / 11 / 12 in d3d9_emu.py, d3d10_emu.py, d3d11_emu.py, d3d12_emu.py).
# Boost Software License 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# draw() takes the vertices of a draw as the vertex stage left them (clip coordinates and attributes),
# clips the primitives against the near and far planes, culls, rasterizes all the primitives of the draw
# at once (numpy: the pixels whose center is inside, the top-left rule on the edges, in batches of about
# a million pixels), interpolates the attributes (perspective-correct, or flat from the provoking vertex),
# runs the fragment stage once on all the fragments of a batch, and merges them into the render targets
# in the order of the primitives (depth test, blending with the factors of Direct3D / OpenGL, write
# masks, several targets). Lines and points are rasterized too (their attributes interpolated).
#
# When the fragment stage needs derivatives (fragment.quads: ddx / ddy in a shader, or the mip level of a
# texture), the fragments come in 2x2 quads, as on a GPU: four lanes per quad (top left, top right,
# bottom left, bottom right), the pixels of a quad the primitive does not cover being helper lanes that
# run the shader (with attributes extrapolated) but write nothing. derivative() takes the differences
# within the quads; sample_mips() chooses the mip levels from the derivatives of the coordinates.
#
# The work of a batch is shared between threads (numpy releases the GIL in its loops): the coverage and the
# attributes of contiguous pieces of the primitives, then, when the fragment stage says it has no side
# effects (fragment.parallel: no unordered access view, no image, no atomic), the fragment stage on pieces
# of the fragments (whole quads); the fragments are merged into the targets in order, by one thread.

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional, Sequence

try:
    import numpy
except ImportError:  # the 3D APIs need numpy (requirements.txt)
    numpy = None

BATCH = 1 << 20  # pixels tested at once
_QUADS = threading.local()  # .lanes: the number of lanes of the fragment stage running in quads (0: none)
PARALLEL = 1 << 15  # the pixels (or fragments) from which a batch is shared between threads
_POOL: List[ThreadPoolExecutor] = []


def threads() -> int:
    """The threads that share a batch: FATHOM_RASTER_THREADS, else up to 4 (memory bound beyond)."""
    try:
        wanted = int(os.environ.get("FATHOM_RASTER_THREADS", "0"))
    except ValueError:
        wanted = 0
    return max(1, wanted if wanted > 0 else min(4, os.cpu_count() or 1))


def _pool(count: int) -> ThreadPoolExecutor:
    if not _POOL or _POOL[0]._max_workers != count:
        _POOL[:] = [ThreadPoolExecutor(count, thread_name_prefix="raster")]
    return _POOL[0]


def _pieces(total: int, count: int, align: int = 1):
    """count ranges covering 0..total, their bounds multiples of align."""
    size = -(-total // count)
    size += -size % align
    return [(start, min(total, start + size)) for start in range(0, total, max(1, size))]


def quad_lanes() -> int:
    return getattr(_QUADS, "lanes", 0)


def derivative(values, axis: str, fine: bool = False):
    """ddx (axis "x") or ddy ("y") of per-lane values (n, ...) of a fragment stage running in quads (zeros
    otherwise): coarse, the same difference for the four lanes of a quad, or fine, per row / column."""
    values = numpy.asarray(values)
    floats = values.astype(numpy.float64) if values.dtype.kind != "f" else values
    n = values.shape[0] if values.ndim else 0
    if n == 0 or n != quad_lanes():
        return numpy.zeros_like(floats)
    q = floats.reshape((n // 4, 4) + floats.shape[1:])
    if axis == "x":
        top = q[:, 1] - q[:, 0]
        bottom = q[:, 3] - q[:, 2] if fine else top
        out = numpy.stack([top, top, bottom, bottom], axis=1)
    else:
        left = q[:, 2] - q[:, 0]
        right = q[:, 3] - q[:, 1] if fine else left
        out = numpy.stack([left, right, left, right], axis=1)
        if getattr(_QUADS, "y_up", False):  # OpenGL: window y goes up, the rows of the quads go down
            out = -out
    return out.reshape(floats.shape)


def implicit_lod(coords, sizes):
    """The mip level (n,) of texture coordinates (n, k) for a texture of sizes (texels per unit along
    each coordinate), from their coarse derivatives; None outside a fragment stage in quads."""
    coords = numpy.asarray(coords, dtype=numpy.float64)
    if coords.ndim != 2 or coords.shape[0] == 0 or coords.shape[0] != quad_lanes():
        return None
    k = min(coords.shape[1], len(sizes))
    scale = numpy.asarray(sizes[:k], dtype=numpy.float64)
    gx = derivative(coords[:, :k], "x") * scale
    gy = derivative(coords[:, :k], "y") * scale
    return lod_of_gradients(gx, gy)


def lod_of_gradients(gx, gy):
    """log2 of the longest of the two gradients (in texels); a very negative level when they are 0."""
    rho = numpy.maximum(numpy.sqrt((numpy.asarray(gx) ** 2).sum(axis=1)), numpy.sqrt((numpy.asarray(gy) ** 2).sum(axis=1)))
    with numpy.errstate(divide="ignore", invalid="ignore"):
        return numpy.where(rho > 0, numpy.log2(numpy.where(rho > 0, rho, 1)), -1000.0)


def mip_levels(lod, count: int, linear: bool):
    """(first level, second level, weight of the second) per lane, for count levels."""
    lod = numpy.clip(numpy.nan_to_num(numpy.asarray(lod, dtype=numpy.float64), nan=0.0, posinf=1000.0, neginf=-1000.0),
                     0, max(count - 1, 0))
    if linear:
        first = numpy.floor(lod).astype(numpy.int64)
        second = numpy.minimum(first + 1, count - 1)
        weight = numpy.where(second > first, lod - first, 0.0)
        return first, second, weight
    first = numpy.minimum(numpy.floor(lod + 0.5).astype(numpy.int64), count - 1)
    return first, first, numpy.zeros(len(lod))


def sample_mips(levels, u, v, lod=None, linear=False, mip_linear=False, address="wrap", border=None):
    """Sampling of a mip chain (a list of (h, w, 4) arrays) at u, v with a level per lane (lod (n,) or None
    for the first level), trilinear with mip_linear."""
    if lod is None or len(levels) <= 1:
        return sample(levels[0], u, v, linear, address, border)
    u, v = numpy.asarray(u, dtype=numpy.float64), numpy.asarray(v, dtype=numpy.float64)
    first, second, weight = mip_levels(lod, len(levels), mip_linear)
    out = numpy.zeros(u.shape + (4,), dtype=numpy.float64)
    for level in numpy.unique(numpy.concatenate([first, second[weight > 0]])):
        texture = levels[int(level)]
        sel = numpy.nonzero(first == level)[0]
        if len(sel):
            out[sel] += sample(texture, u[sel], v[sel], linear, address, border) * (1 - weight[sel])[:, None]
        sel = numpy.nonzero((second == level) & (weight > 0))[0]
        if len(sel):
            out[sel] += sample(texture, u[sel], v[sel], linear, address, border) * weight[sel][:, None]
    return out


class Framebuffer:
    """A color buffer (RGBA bytes) and its depth buffer."""

    def __init__(self, width: int, height: int):
        self.width, self.height = max(1, int(width)), max(1, int(height))
        self.color = numpy.zeros((self.height, self.width, 4), dtype=numpy.uint8)
        self.color[..., 3] = 255
        self.depth = numpy.ones((self.height, self.width), dtype=numpy.float32)

    def resize(self, width: int, height: int):
        if (width, height) != (self.width, self.height) and width > 0 and height > 0:
            self.__init__(width, height)

    def clear(self, rgba: Optional[Sequence[float]] = None, depth: Optional[float] = None, rect=None):
        x0, y0, x1, y1 = rect if rect is not None else (0, 0, self.width, self.height)
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(self.width, x1), min(self.height, y1)
        if x0 >= x1 or y0 >= y1:
            return
        if rgba is not None:
            self.color[y0:y1, x0:x1] = [round(max(0.0, min(1.0, c)) * 255) for c in rgba]
        if depth is not None:
            self.depth[y0:y1, x0:x1] = depth

    def image(self):
        from PIL import Image
        return Image.fromarray(self.color, "RGBA").convert("RGB")


# ------------------------------------------------------------------ output merger
class Blend:
    """The blending of a render target, with the numbers of Direct3D (D3D11_BLEND / D3D11_BLEND_OP)."""

    def __init__(self, enabled=False, src=2, dst=1, op=1, src_a=2, dst_a=1, op_a=1, write_mask=0xF):
        self.enabled, self.src, self.dst, self.op = enabled, src, dst, op
        self.src_a, self.dst_a, self.op_a, self.write_mask = src_a, dst_a, op_a, write_mask


def _factor(code, src, dst, constant):
    a_s, a_d = numpy.repeat(src[..., 3:4], 4, axis=-1), numpy.repeat(dst[..., 3:4], 4, axis=-1)
    if code == 1:
        return numpy.zeros_like(src)
    if code == 2:
        return numpy.ones_like(src)
    if code == 11:
        saturate = numpy.minimum(a_s, 1 - a_d)
        saturate[..., 3] = 1
        return saturate
    constant = numpy.broadcast_to(numpy.asarray(constant, dtype=numpy.float32), src.shape)
    return {3: src, 4: 1 - src, 5: a_s, 6: 1 - a_s, 7: a_d, 8: 1 - a_d, 9: dst, 10: 1 - dst, 14: constant,
            15: 1 - constant, 16: src, 17: 1 - src, 18: a_s, 19: 1 - a_s}.get(code, numpy.ones_like(src))


def _combine(op, a, b):
    return {2: a - b, 3: b - a, 4: numpy.minimum(a, b), 5: numpy.maximum(a, b)}.get(op, a + b)


def blend_colors(blend: Optional[Blend], src, dst, constant=(1.0, 1.0, 1.0, 1.0)):
    """src, dst: (n, 4) floats 0..1; the blended colors."""
    if blend is None or not blend.enabled:
        return src
    if blend.op in (4, 5):
        color = _combine(blend.op, src, dst)
    else:
        color = _combine(blend.op, src * _factor(blend.src, src, dst, constant), dst * _factor(blend.dst, src, dst, constant))
    if blend.op_a in (4, 5):
        alpha = _combine(blend.op_a, src, dst)
    else:
        alpha = _combine(blend.op_a, src * _factor(blend.src_a, src, dst, constant), dst * _factor(blend.dst_a, src, dst, constant))
    return numpy.concatenate([color[:, :3], alpha[:, 3:4]], axis=1)


DEPTH_TESTS = {1: lambda z, d: numpy.zeros(z.shape, bool), 2: numpy.less, 3: numpy.equal, 4: numpy.less_equal,
               5: numpy.greater, 6: numpy.not_equal, 7: numpy.greater_equal, 8: lambda z, d: numpy.ones(z.shape, bool)}


class Target:
    """A render target: color (h, w, 4) array (uint8 0..255, or float32 for float formats), its blend."""

    def __init__(self, color, blend: Optional[Blend] = None, write_mask: int = 0xF, srgb=False):
        self.color, self.blend, self.srgb = color, blend, srgb
        self.write_mask = blend.write_mask if blend is not None else write_mask


class Output:
    """Where the fragments go: targets, depth buffer and test (Direct3D's numbers), blend constant."""

    def __init__(self, targets: List[Target], depth=None, depth_func=0, depth_write=False, constant=(1.0, 1.0, 1.0, 1.0),
                 scissor=None, stencil=None):
        self.targets, self.depth, self.depth_func, self.depth_write = targets, depth, depth_func, depth_write
        self.constant, self.scissor = constant, scissor
        shape = targets[0].color.shape[:2] if targets else (depth.shape if depth is not None else (1, 1))
        self.height, self.width = shape


def merge(out: Output, x, y, z, colors: List, alive=None):
    """Writes fragments (pixel x, y in the order of the primitives) into the targets: the k-th fragment of
    each pixel in pass k, so that the depth test and blending see the earlier ones."""
    if len(x) == 0:
        return
    if alive is not None:
        keep = numpy.nonzero(alive)[0]
        if len(keep) != len(x):
            x, y, z = x[keep], y[keep], z[keep] if z is not None else None
            colors = [c[keep] if c is not None else None for c in colors]
            if len(x) == 0:
                return
    key = y.astype(numpy.int64) * out.width + x
    order = numpy.argsort(key, kind="stable")
    sorted_key = key[order]
    starts = numpy.ones(len(order), dtype=bool)
    starts[1:] = sorted_key[1:] != sorted_key[:-1]
    group_start = numpy.maximum.accumulate(numpy.where(starts, numpy.arange(len(order)), 0))
    rank = numpy.empty(len(order), dtype=numpy.int64)
    rank[order] = numpy.arange(len(order)) - group_start
    layers = int(rank.max()) + 1
    for layer in range(layers):
        index = numpy.arange(len(x)) if layers == 1 else numpy.nonzero(rank == layer)[0]
        px, py = x[index], y[index]
        if out.depth_func and out.depth is not None and z is not None:
            pz = z[index]
            passed = DEPTH_TESTS.get(out.depth_func, numpy.less_equal)(pz, out.depth[py, px])
            if not passed.all():
                index, px, py, pz = index[passed], px[passed], py[passed], pz[passed]
            if out.depth_write:
                out.depth[py, px] = pz
        if len(px) == 0:
            continue
        for target, color in zip(out.targets, colors):
            if target is not None and color is not None:
                _write(target, px, py, numpy.asarray(color)[index], out.constant)


def _linear(values):
    out = numpy.array(values, dtype=numpy.float32, copy=True)
    c = numpy.clip(out[:, :3], 0, 1)
    out[:, :3] = numpy.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return out


def _srgb(values):
    out = numpy.array(values, dtype=numpy.float32, copy=True)
    c = numpy.clip(out[:, :3], 0, 1)
    out[:, :3] = numpy.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)
    return out


def _write(target: Target, px, py, src, constant):
    buffer = target.color
    floating = buffer.dtype != numpy.uint8
    src = numpy.nan_to_num(numpy.asarray(src, dtype=numpy.float32))
    if not floating:
        src = numpy.clip(src, 0, 1)
    if target.blend is not None and target.blend.enabled:
        dst = buffer[py, px].astype(numpy.float32)
        if not floating:
            dst = dst / 255
        if target.srgb:
            dst = _linear(dst)
        src = blend_colors(target.blend, src, dst, constant)
        if not floating:
            src = numpy.clip(src, 0, 1)
    if target.srgb:
        src = _srgb(numpy.clip(src, 0, 1))
    channels = buffer.shape[2]
    values = src[:, :channels] if floating else numpy.round(src[:, :channels] * 255).astype(numpy.uint8)
    mask = target.write_mask
    if mask & ((1 << channels) - 1) == (1 << channels) - 1:
        buffer[py, px] = values
    else:
        for c in range(channels):
            if mask >> c & 1:
                buffer[py, px, c] = values[:, c]


# ------------------------------------------------------------------ fragments
class Fragments:
    """What the fragment stage receives: n fragments, their pixel, depth, 1/w, primitive, facing, and the
    interpolated attributes (n, k)."""

    def __init__(self, x, y, z, inv_w, prim, front, attrs, helper=None):
        self.x, self.y, self.z, self.inv_w, self.prim, self.front, self.attrs = x, y, z, inv_w, prim, front, attrs
        self.n = len(x)
        self.helper = helper        # (n,) True for the helper lanes of the quads (None: no quads)

    def part(self, start: int, end: int) -> "Fragments":
        return Fragments(self.x[start:end], self.y[start:end], self.z[start:end], self.inv_w[start:end],
                         self.prim[start:end], self.front[start:end], self.attrs[start:end],
                         self.helper[start:end] if self.helper is not None else None)


FragmentStage = Callable[[Fragments], tuple]  # -> (colors per target (n, 4), alive (n,) or None, depth (n,) or None)


class Pipeline:
    """The fixed stages between the vertex stage and the fragment stage. convention: "d3d" (z in 0..w,
    y down) or "gl" (z in -w..w, y up); offset: 0.5 for Direct3D 9 (pixel centers at integers)."""

    def __init__(self, viewport, convention="d3d", depth_range=(0.0, 1.0), offset=0.0, cull="none", front_cw=True,
                 provoking="first", point_size=1.0, line_width=1.0, scissor=None, clip_depth=True):
        self.viewport = viewport              # x, y, width, height (y from the top, or from the bottom in gl)
        self.convention, self.depth_range, self.offset = convention, depth_range, offset
        self.cull, self.front_cw, self.provoking = cull, front_cw, provoking
        self.point_size, self.line_width, self.scissor, self.clip_depth = point_size, line_width, scissor, clip_depth


def _clip_polygon(points, attrs, planes):
    """Sutherland-Hodgman in clip coordinates: points (k, 4), attrs (k, m); planes: functions of a point
    giving a distance (>= 0 inside)."""
    for plane in planes:
        if len(points) == 0:
            break
        distance = plane(points)
        out_p, out_a = [], []
        count = len(points)
        for i in range(count):
            j = (i + 1) % count
            di, dj = distance[i], distance[j]
            if di >= 0:
                out_p.append(points[i])
                out_a.append(attrs[i])
            if (di >= 0) != (dj >= 0):
                t = di / (di - dj)
                out_p.append(points[i] + t * (points[j] - points[i]))
                out_a.append(attrs[i] + t * (attrs[j] - attrs[i]))
        points = numpy.array(out_p) if out_p else numpy.zeros((0, 4))
        attrs = numpy.array(out_a) if out_a else numpy.zeros((0, attrs.shape[1]))
    return points, attrs


def _planes(pipeline: Pipeline):
    eps = 1e-6
    planes = [lambda p: p[:, 3] - eps]
    if pipeline.convention == "gl":
        planes.append(lambda p: p[:, 2] + p[:, 3])          # near: z >= -w
        if pipeline.clip_depth:
            planes.append(lambda p: p[:, 3] - p[:, 2])      # far: z <= w
    else:
        planes.append(lambda p: p[:, 2])                    # near: z >= 0
        if pipeline.clip_depth:
            planes.append(lambda p: p[:, 3] - p[:, 2])
    return planes


def _inside(clip, pipeline):
    w = clip[:, 3]
    ok = w > 1e-6
    if pipeline.convention == "gl":
        ok &= clip[:, 2] >= -w
    else:
        ok &= clip[:, 2] >= 0
    if pipeline.clip_depth:
        ok &= clip[:, 2] <= w
    return ok


def clip_primitives(clip, attrs, prims, pipeline: Pipeline, flat_mask=None):
    """The primitives (p, k) after clipping against w > 0 and the near / far planes: new vertices are
    appended. Triangles become fans; flat attributes keep the provoking vertex's value."""
    inside = _inside(clip, pipeline)
    whole = inside[prims].all(axis=1)
    if whole.all():
        return clip, attrs, prims, numpy.arange(len(prims))
    keep = numpy.nonzero(whole)[0]
    new_clip, new_attrs, new_prims, source = [clip], [attrs], [prims[keep]], [keep]
    total = len(clip)
    planes = _planes(pipeline)
    k = prims.shape[1]
    for p in numpy.nonzero(~whole & inside[prims].any(axis=1))[0] if k > 1 else []:
        corners = prims[p]
        points, values = _clip_polygon(clip[corners], attrs[corners], planes)
        if k == 2 and len(points) >= 2:
            points, values = points[:2], values[:2]
        if len(points) < k:
            continue
        if flat_mask is not None and flat_mask.any():
            provoking = corners[0] if pipeline.provoking == "first" else corners[-1]
            values[:, flat_mask] = attrs[provoking, flat_mask]
        n = len(points)
        new_clip.append(points)
        new_attrs.append(values)
        if k == 2:
            new_prims.append(numpy.array([[total, total + 1]]))
            source.append(numpy.array([p]))
        else:
            fan = numpy.array([[total, total + i, total + i + 1] for i in range(1, n - 1)])
            new_prims.append(fan)
            source.append(numpy.full(len(fan), p))
        total += n
    return (numpy.concatenate(new_clip), numpy.concatenate(new_attrs), numpy.concatenate(new_prims),
            numpy.concatenate(source))


def to_screen(clip, pipeline: Pipeline, height: int):
    """(x, y down, z, 1/w) of clip coordinates."""
    x0, y0, width, h = pipeline.viewport
    w = clip[:, 3]
    inv = numpy.where(numpy.abs(w) > 1e-12, 1 / numpy.where(w == 0, 1, w), 0)
    ndc = clip[:, :3] * inv[:, None]
    near, far = pipeline.depth_range
    sx = x0 + (ndc[:, 0] + 1) * width / 2 - pipeline.offset
    if pipeline.convention == "gl":
        sy = height - (y0 + (ndc[:, 1] + 1) * h / 2)
        sz = near + (ndc[:, 2] + 1) / 2 * (far - near)
    else:
        sy = y0 + (1 - ndc[:, 1]) * h / 2 - pipeline.offset
        sz = near + ndc[:, 2] * (far - near)
    return numpy.stack([sx, sy, sz, inv], axis=1)


def draw(out: Output, pipeline: Pipeline, kind: str, prims, attrs, fragment: FragmentStage, clip=None, screen=None,
         flat_mask=None):
    """kind: "triangles" (prims (p, 3)), "lines" (p, 2) or "points" (p, 1). Either clip (n, 4) clip
    coordinates or screen (n, 4) = x, y, z, 1/w already on the screen (pretransformed vertices).
    attrs (n, m) floats. Returns the number of fragments written."""
    prims = numpy.asarray(prims, dtype=numpy.int64).reshape(-1, {"triangles": 3, "lines": 2, "points": 1}[kind])
    attrs = numpy.asarray(attrs, dtype=numpy.float64)
    if attrs.ndim == 1:
        attrs = attrs.reshape(-1, 0) if attrs.size == 0 else attrs[:, None]
    if len(prims) == 0:
        return 0
    source = numpy.arange(len(prims))
    if screen is None:
        clip = numpy.asarray(clip, dtype=numpy.float64)
        if kind == "points":
            ok = _inside(clip, pipeline)[prims[:, 0]]
            prims, source = prims[ok], source[ok]
        else:
            clip, attrs, prims, source = clip_primitives(clip, attrs, prims, pipeline, flat_mask)
        screen = to_screen(clip, pipeline, out.height)
    else:
        screen = numpy.asarray(screen, dtype=numpy.float64)
    if kind == "triangles":
        return _triangles(out, pipeline, screen, attrs, prims, source, fragment, flat_mask)
    return _lines_points(out, pipeline, kind, screen, attrs, prims, source, fragment, flat_mask)


def _bounds(out: Output, pipeline: Pipeline):
    left, top, right, bottom = 0, 0, out.width, out.height
    for rect in (pipeline.scissor, out.scissor):
        if rect is not None:
            left, top = max(left, rect[0]), max(top, rect[1])
            right, bottom = min(right, rect[2]), min(bottom, rect[3])
    return left, top, right, bottom


def _triangles(out, pipeline, screen, attrs, prims, source, fragment, flat_mask):
    a, b, c = screen[prims[:, 0]], screen[prims[:, 1]], screen[prims[:, 2]]
    area = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])  # > 0: clockwise
    finite = numpy.isfinite(area) & (numpy.abs(area) > 1e-12)
    front = (area > 0) == pipeline.front_cw
    keep = finite.copy()
    if pipeline.cull == "back":
        keep &= front
    elif pipeline.cull == "front":
        keep &= ~front
    left, top, right, bottom = _bounds(out, pipeline)
    xs = numpy.stack([a[:, 0], b[:, 0], c[:, 0]], axis=1)
    ys = numpy.stack([a[:, 1], b[:, 1], c[:, 1]], axis=1)
    with numpy.errstate(invalid="ignore"):
        x0 = numpy.maximum(left, numpy.floor(numpy.nan_to_num(xs.min(axis=1), nan=0, posinf=1e9, neginf=-1e9)).astype(numpy.int64))
        x1 = numpy.minimum(right, numpy.ceil(numpy.nan_to_num(xs.max(axis=1), nan=0, posinf=1e9, neginf=-1e9)).astype(numpy.int64) + 1)
        y0 = numpy.maximum(top, numpy.floor(numpy.nan_to_num(ys.min(axis=1), nan=0, posinf=1e9, neginf=-1e9)).astype(numpy.int64))
        y1 = numpy.minimum(bottom, numpy.ceil(numpy.nan_to_num(ys.max(axis=1), nan=0, posinf=1e9, neginf=-1e9)).astype(numpy.int64) + 1)
    keep &= (x1 > x0) & (y1 > y0)
    ids = numpy.nonzero(keep)[0]
    if len(ids) == 0:
        return 0
    # pieces: a triangle's box, cut into bands of rows when it is large
    widths = (x1 - x0)[ids]
    heights = (y1 - y0)[ids]
    rows_per_piece = numpy.maximum(1, BATCH // numpy.maximum(widths, 1))
    pieces = (heights + rows_per_piece - 1) // rows_per_piece
    piece_tri = numpy.repeat(ids, pieces)
    first_piece = numpy.repeat(numpy.cumsum(pieces) - pieces, pieces)
    index_in_tri = numpy.arange(len(piece_tri)) - first_piece
    piece_y0 = y0[piece_tri] + index_in_tri * numpy.repeat(rows_per_piece, pieces)
    piece_y1 = numpy.minimum(y1[piece_tri], piece_y0 + numpy.repeat(rows_per_piece, pieces))
    piece_x0, piece_x1 = x0[piece_tri], x1[piece_tri]
    sizes = (piece_x1 - piece_x0) * (piece_y1 - piece_y0)
    # top-left rule: an edge whose line goes through a pixel center covers it if it is a top or left edge
    edges = []
    for (p, q) in ((b, c), (c, a), (a, b)):
        dx, dy = q[:, 0] - p[:, 0], q[:, 1] - p[:, 1]
        sign = numpy.where(area > 0, 1.0, -1.0)
        # with the triangle made clockwise (y down): a top edge goes right horizontally, a left edge goes up
        top_left = numpy.where(sign > 0, ((dy == 0) & (dx > 0)) | (dy < 0), ((dy == 0) & (dx < 0)) | (dy > 0))
        edges.append(top_left)
    written = 0
    start = 0
    count = len(piece_tri)
    cumulative = numpy.cumsum(sizes)
    while start < count:
        limit = (cumulative[start - 1] if start else 0) + BATCH
        end = max(start + 1, int(numpy.searchsorted(cumulative, limit, side="right")))
        written += _batch(out, pipeline, screen, attrs, prims, source, fragment, flat_mask, area, edges, front,
                          piece_tri[start:end], piece_x0[start:end], piece_x1[start:end], piece_y0[start:end],
                          piece_y1[start:end], sizes[start:end])
        start = end
    return written


def _batch(out, pipeline, screen, attrs, prims, source, fragment, flat_mask, area, edges, front, tri, px0, px1, py0, py1,
           sizes):
    total = int(sizes.sum())
    if total == 0:
        return 0
    quads = bool(getattr(fragment, "quads", False))
    count = threads()
    if count > 1 and total >= PARALLEL and len(tri) > 1:
        # contiguous pieces of the primitives (their order kept), about as many pixels each
        ends = numpy.cumsum(sizes)
        cuts = sorted({int(c) for c in numpy.searchsorted(ends, numpy.arange(1, count) * total / count)} | {0, len(tri)})
        ranges = [(a, b) for a, b in zip(cuts, cuts[1:]) if b > a]
        parts = list(_pool(count).map(lambda r: _cover(out, pipeline, screen, attrs, prims, flat_mask, area, edges, quads,
                                                       tri[r[0]:r[1]], px0[r[0]:r[1]], px1[r[0]:r[1]], py0[r[0]:r[1]],
                                                       py1[r[0]:r[1]], sizes[r[0]:r[1]]), ranges))
        parts = [p for p in parts if p is not None]
        if not parts:
            return 0
        x, y, t, z, inv_w, values, helper = (numpy.concatenate([p[k] for p in parts]) if parts[0][k] is not None else None
                                             for k in range(7))
    else:
        covered = _cover(out, pipeline, screen, attrs, prims, flat_mask, area, edges, quads, tri, px0, px1, py0, py1, sizes)
        if covered is None:
            return 0
        x, y, t, z, inv_w, values, helper = covered
    frags = Fragments(x, y, z, inv_w, source[t], front[t], values, helper)
    colors, alive, depth = _fragments(fragment, frags, quads, pipeline.convention == "gl", count)
    if helper is not None:
        alive = ~helper if alive is None else (numpy.asarray(alive, dtype=bool) & ~helper)
    if depth is not None:
        z = numpy.asarray(depth, dtype=numpy.float64)
    merge(out, x, y, z.astype(numpy.float32), colors, alive)
    return len(x) if helper is None else int((~helper).sum())


def _fragments(fragment, frags: Fragments, quads: bool, y_up: bool, count: int):
    """The fragment stage on frags: on pieces (whole quads) in threads when it allows it."""
    def run(part):
        if not quads:
            return fragment(part)
        _QUADS.lanes, _QUADS.y_up = part.n, y_up
        try:
            return fragment(part)
        finally:
            _QUADS.lanes = 0
    if count < 2 or frags.n < PARALLEL or not getattr(fragment, "parallel", False):
        return run(frags)
    ranges = _pieces(frags.n, count, 4)
    results = list(_pool(count).map(lambda r: run(frags.part(*r)), ranges))
    targets = max(len(r[0]) for r in results)
    colors = []
    for k in range(targets):
        items = [r[0][k] if k < len(r[0]) else None for r in results]
        if all(item is None for item in items):
            colors.append(None)
            continue
        shape = next(item for item in items if item is not None).shape[1:]
        colors.append(numpy.concatenate([item if item is not None else numpy.zeros((b - a,) + shape, dtype=numpy.float32)
                                         for item, (a, b) in zip(items, ranges)]))
    alive = None
    if any(r[1] is not None for r in results):
        alive = numpy.concatenate([numpy.asarray(r[1], dtype=bool) if r[1] is not None else numpy.ones(b - a, dtype=bool)
                                   for r, (a, b) in zip(results, ranges)])
    depth = None
    if any(r[2] is not None for r in results):
        depth = numpy.concatenate([numpy.asarray(r[2], dtype=numpy.float64) if r[2] is not None else frags.z[a:b]
                                   for r, (a, b) in zip(results, ranges)])
    return colors, alive, depth


def _cover(out, pipeline, screen, attrs, prims, flat_mask, area, edges, quads, tri, px0, px1, py0, py1, sizes):
    """The fragments of the pixels the primitives tri cover (in their order): x, y, primitive, z, 1/w, attributes,
    helper lanes (quads); None when none."""
    total = int(sizes.sum())
    if total == 0:
        return None
    owner = numpy.repeat(numpy.arange(len(tri)), sizes)
    offset = numpy.arange(total) - numpy.repeat(numpy.cumsum(sizes) - sizes, sizes)
    width = (px1 - px0)[owner]
    x = px0[owner] + offset % width
    y = py0[owner] + offset // width
    t = tri[owner]
    corner = prims[t]
    a, b, c = screen[corner[:, 0]], screen[corner[:, 1]], screen[corner[:, 2]]
    cx, cy = x + 0.5, y + 0.5
    ar = area[t]
    e0 = (b[:, 0] - cx) * (c[:, 1] - cy) - (b[:, 1] - cy) * (c[:, 0] - cx)
    e1 = (c[:, 0] - cx) * (a[:, 1] - cy) - (c[:, 1] - cy) * (a[:, 0] - cx)
    e2 = (a[:, 0] - cx) * (b[:, 1] - cy) - (a[:, 1] - cy) * (b[:, 0] - cx)
    sign = numpy.where(ar > 0, 1.0, -1.0)
    e0, e1, e2 = e0 * sign, e1 * sign, e2 * sign
    inside = ((e0 > 0) | ((e0 == 0) & edges[0][t])) & ((e1 > 0) | ((e1 == 0) & edges[1][t])) & \
        ((e2 > 0) | ((e2 == 0) & edges[2][t]))
    sel = numpy.nonzero(inside)[0]
    if len(sel) == 0:
        return None
    helper = None
    if quads:
        # the 2x2 quads of the covered pixels, four lanes each; those not covered are helpers
        x, y, t = x[sel], y[sel], t[sel]
        left, top, right, bottom = _bounds(out, pipeline)
        key = numpy.unique(numpy.stack([t, y >> 1, x >> 1], axis=1), axis=0)
        t = numpy.repeat(key[:, 0], 4)
        x = numpy.repeat(key[:, 2] * 2, 4) + numpy.tile([0, 1, 0, 1], len(key))
        y = numpy.repeat(key[:, 1] * 2, 4) + numpy.tile([0, 0, 1, 1], len(key))
        corner = prims[t]
        a, b, c = screen[corner[:, 0]], screen[corner[:, 1]], screen[corner[:, 2]]
        cx, cy = x + 0.5, y + 0.5
        ar = area[t]
        e0 = (b[:, 0] - cx) * (c[:, 1] - cy) - (b[:, 1] - cy) * (c[:, 0] - cx)
        e1 = (c[:, 0] - cx) * (a[:, 1] - cy) - (c[:, 1] - cy) * (a[:, 0] - cx)
        e2 = (a[:, 0] - cx) * (b[:, 1] - cy) - (a[:, 1] - cy) * (b[:, 0] - cx)
        sign = numpy.where(ar > 0, 1.0, -1.0)
        e0, e1, e2 = e0 * sign, e1 * sign, e2 * sign
        covered = ((e0 > 0) | ((e0 == 0) & edges[0][t])) & ((e1 > 0) | ((e1 == 0) & edges[1][t])) & \
            ((e2 > 0) | ((e2 == 0) & edges[2][t])) & (x >= left) & (x < right) & (y >= top) & (y < bottom)
        helper = ~covered
        sel = numpy.arange(len(x))
    x, y, t = x[sel], y[sel], t[sel]
    a, b, c = a[sel], b[sel], c[sel]
    ar = numpy.abs(ar[sel])
    w0, w1 = e0[sel] / ar, e1[sel] / ar
    w2 = 1.0 - w0 - w1
    z = w0 * a[:, 2] + w1 * b[:, 2] + w2 * c[:, 2]
    p0, p1, p2 = w0 * a[:, 3], w1 * b[:, 3], w2 * c[:, 3]
    inv_w = p0 + p1 + p2
    safe = numpy.where(inv_w == 0, 1, inv_w)
    p0, p1, p2 = p0 / safe, p1 / safe, p2 / safe
    corner = prims[t]
    values = p0[:, None] * attrs[corner[:, 0]] + p1[:, None] * attrs[corner[:, 1]] + p2[:, None] * attrs[corner[:, 2]]
    if flat_mask is not None and flat_mask.any():
        provoking = corner[:, 0] if pipeline.provoking == "first" else corner[:, 2]
        values[:, flat_mask] = attrs[provoking][:, flat_mask]
    return x, y, t, z, inv_w, values, helper


def _lines_points(out, pipeline, kind, screen, attrs, prims, source, fragment, flat_mask):
    left, top, right, bottom = _bounds(out, pipeline)
    xs, ys, ts, owners = [], [], [], []
    if kind == "lines":
        half = max(0.5, pipeline.line_width / 2)
        for i, (p, q) in enumerate(prims):
            (ax, ay), (bx, by) = screen[p, :2], screen[q, :2]
            if not numpy.isfinite([ax, ay, bx, by]).all():
                continue
            steps = int(max(abs(bx - ax), abs(by - ay))) + 1
            if steps > 1 << 16:
                continue
            t = (numpy.arange(steps) + 0.5) / steps
            px, py = ax + (bx - ax) * t, ay + (by - ay) * t
            if half > 0.5:  # wide lines: the pixels across, along the minor axis
                spread = numpy.arange(-int(half) + 1, int(half) + 1) if half >= 1 else numpy.array([0])
                horizontal = abs(bx - ax) >= abs(by - ay)
                px = numpy.repeat(px, len(spread)) + (0 if horizontal else numpy.tile(spread, steps))
                py = numpy.repeat(py, len(spread)) + (numpy.tile(spread, steps) if horizontal else 0)
                t = numpy.repeat(t, len(spread))
            xs.append(numpy.floor(px).astype(numpy.int64))
            ys.append(numpy.floor(py).astype(numpy.int64))
            ts.append(t)
            owners.append(numpy.full(len(t), i))
    else:
        size = max(1.0, pipeline.point_size)
        for i, (p,) in enumerate(prims):
            cx, cy = screen[p, :2]
            if not numpy.isfinite([cx, cy]).all():
                continue
            xr = numpy.arange(int(numpy.floor(cx - size / 2 + 0.5)), int(numpy.floor(cx + size / 2 + 0.5)))
            yr = numpy.arange(int(numpy.floor(cy - size / 2 + 0.5)), int(numpy.floor(cy + size / 2 + 0.5)))
            if len(xr) == 0:
                xr = numpy.array([int(numpy.floor(cx))])
            if len(yr) == 0:
                yr = numpy.array([int(numpy.floor(cy))])
            gx, gy = numpy.meshgrid(xr, yr)
            xs.append(gx.ravel())
            ys.append(gy.ravel())
            ts.append(numpy.zeros(gx.size))
            owners.append(numpy.full(gx.size, i))
    if not xs:
        return 0
    x, y, t, owner = numpy.concatenate(xs), numpy.concatenate(ys), numpy.concatenate(ts), numpy.concatenate(owners)
    ok = (x >= left) & (x < right) & (y >= top) & (y < bottom)
    x, y, t, owner = x[ok], y[ok], t[ok], owner[ok]
    if len(x) == 0:
        return 0
    first = prims[owner, 0]
    last = prims[owner, -1]
    sa, sb = screen[first], screen[last]
    # perspective-correct along the line
    pa, pb = (1 - t) * sa[:, 3], t * sb[:, 3]
    inv_w = pa + pb
    safe = numpy.where(inv_w == 0, 1, inv_w)
    pa, pb = pa / safe, pb / safe
    z = (1 - t) * sa[:, 2] + t * sb[:, 2]
    values = pa[:, None] * attrs[first] + pb[:, None] * attrs[last]
    if flat_mask is not None and flat_mask.any():
        provoking = first if pipeline.provoking == "first" else last
        values[:, flat_mask] = attrs[provoking][:, flat_mask]
    frags = Fragments(x, y, z, inv_w, source[owner], numpy.ones(len(x), dtype=bool), values)
    colors, alive, depth = fragment(frags)
    if depth is not None:
        z = numpy.asarray(depth, dtype=numpy.float64)
    merge(out, x, y, z.astype(numpy.float32), colors, alive)
    return len(x)


# ------------------------------------------------------------------ textures
def _address(coord, size: int, mode: str):
    if mode in ("clamp", "border"):
        return numpy.clip(coord, 0, size - 1)
    if mode == "mirror":
        period = numpy.mod(coord, 2 * size)
        return numpy.where(period < size, period, 2 * size - 1 - period)
    return numpy.mod(coord, size)


def sample(texture, u, v, linear=False, address="wrap", border=None):
    """The texels of texture ((h, w, 4) floats) at the coordinates u, v (0..1, arrays of any shape)."""
    h, w = texture.shape[:2]
    u = numpy.nan_to_num(numpy.asarray(u, dtype=numpy.float64))
    v = numpy.nan_to_num(numpy.asarray(v, dtype=numpy.float64))
    if isinstance(address, tuple):
        address_u, address_v = address
    else:
        address_u = address_v = address
    if not linear:
        xi, yi = numpy.floor(u * w).astype(numpy.int64), numpy.floor(v * h).astype(numpy.int64)
        result = texture[_address(yi, h, address_v), _address(xi, w, address_u)]
        if border is not None and (address_u == "border" or address_v == "border"):
            outside = ((xi < 0) | (xi >= w)) & (address_u == "border") | ((yi < 0) | (yi >= h)) & (address_v == "border")
            result = numpy.where(outside[..., None], numpy.asarray(border, dtype=texture.dtype), result)
        return result
    fx, fy = u * w - 0.5, v * h - 0.5
    x0, y0 = numpy.floor(fx).astype(numpy.int64), numpy.floor(fy).astype(numpy.int64)
    tx, ty = (fx - x0)[..., None], (fy - y0)[..., None]
    xa, xb = _address(x0, w, address_u), _address(x0 + 1, w, address_u)
    ya, yb = _address(y0, h, address_v), _address(y0 + 1, h, address_v)
    top = texture[ya, xa] * (1 - tx) + texture[ya, xb] * tx
    bottom = texture[yb, xa] * (1 - tx) + texture[yb, xb] * tx
    return top * (1 - ty) + bottom * ty


def primitives(kind: str, count: int):
    """The vertex indices (p, k) of the triangles, lines or points of a primitive of count vertices.
    kind: triangles, strip, fan, quads, quad_strip, polygon, lines, line_strip, line_loop, points."""
    i = numpy.arange(max(count, 0))
    if kind == "triangles":
        n = count // 3
        return i[:3 * n].reshape(n, 3)
    if kind == "strip":
        n = max(0, count - 2)
        base = numpy.arange(n)
        even = base % 2 == 0
        return numpy.stack([numpy.where(even, base, base + 1), numpy.where(even, base + 1, base), base + 2], axis=1)
    if kind in ("fan", "polygon"):
        n = max(0, count - 2)
        base = numpy.arange(1, n + 1)
        return numpy.stack([numpy.zeros(n, dtype=numpy.int64), base, base + 1], axis=1)
    if kind == "quads":
        q = numpy.arange(count // 4) * 4
        return numpy.stack([numpy.stack([q, q + 1, q + 2], 1), numpy.stack([q, q + 2, q + 3], 1)], 1).reshape(-1, 3)
    if kind == "quad_strip":
        q = numpy.arange(max(0, (count - 2) // 2)) * 2
        return numpy.stack([numpy.stack([q, q + 1, q + 3], 1), numpy.stack([q, q + 3, q + 2], 1)], 1).reshape(-1, 3)
    if kind == "lines":
        n = count // 2
        return i[:2 * n].reshape(n, 2)
    if kind == "line_strip":
        n = max(0, count - 1)
        return numpy.stack([numpy.arange(n), numpy.arange(n) + 1], axis=1)
    if kind == "line_loop":
        if count < 2:
            return numpy.zeros((0, 2), dtype=numpy.int64)
        return numpy.stack([i, (i + 1) % count], axis=1)
    return i.reshape(-1, 1)


def kind_of(kind: str) -> str:
    """The rasterized kind of a primitive kind of primitives()."""
    if kind in ("lines", "line_strip", "line_loop"):
        return "lines"
    if kind == "points":
        return "points"
    return "triangles"
