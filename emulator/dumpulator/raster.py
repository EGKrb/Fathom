# Fathom addition to dumpulator: a small software rasterizer for the 3D APIs the emulator implements
# (OpenGL 1.x in opengl_emu.py, Direct3D 9 / 11 in d3d_emu.py). Boost Software License 1.0, like the
# rest of dumpulator (LICENSE.dumpulator).
#
# A framebuffer of RGBA bytes and a depth buffer (numpy). Triangles are sampled at the pixel centers
# (the coverage rule of OpenGL and Direct3D), their colors interpolated from the vertices (Gouraud,
# perspective-correct when the vertices give 1/w) or flat, optionally modulated by a texture (nearest or
# bilinear, wrap / mirror / clamp), with an optional depth test, back-face culling and alpha blending
# (source alpha). Lines and points are drawn with Pillow on top (no depth). cover() gives the covered
# pixels and their barycentric weights to the shader interpreter of Direct3D 11 (dxbc.py).

from typing import List, Optional, Sequence, Tuple

try:
    import numpy
except ImportError:  # the 3D APIs need numpy (requirements.txt)
    numpy = None

Vertex = Tuple[float, float, float, Sequence[float]]  # x, y in pixels (y down), z in 0..1, RGBA 0..1


class Framebuffer:
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

    def cover(self, a, b, c, cull: int = 0, scissor=None):
        """The pixels whose center is in the triangle a, b, c ((x, y, z) in pixels, y down): (y0, x0,
        inside mask, w0, w1, w2, z) or None. cull: 0 none, 1 the counterclockwise triangles (on the
        screen), 2 the clockwise ones."""
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = a[:3], b[:3], c[:3]
        area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)  # > 0: clockwise on the screen (y down)
        if abs(area) < 1e-12 or not all(numpy.isfinite([ax, ay, bx, by, cx, cy])):
            return None
        if (cull == 1 and area < 0) or (cull == 2 and area > 0):
            return None
        left, top, right, bottom = scissor if scissor is not None else (0, 0, self.width, self.height)
        x0 = max(max(0, left), int(numpy.floor(min(ax, bx, cx))))
        x1 = min(min(self.width, right), int(numpy.ceil(max(ax, bx, cx))) + 1)
        y0 = max(max(0, top), int(numpy.floor(min(ay, by, cy))))
        y1 = min(min(self.height, bottom), int(numpy.ceil(max(ay, by, cy))) + 1)
        if x0 >= x1 or y0 >= y1:
            return None
        ys, xs = numpy.mgrid[y0:y1, x0:x1].astype(numpy.float64)
        px, py = xs + 0.5, ys + 0.5
        w0 = ((bx - px) * (cy - py) - (by - py) * (cx - px)) / area
        w1 = ((cx - px) * (ay - py) - (cy - py) * (ax - px)) / area
        w2 = 1.0 - w0 - w1
        inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        if not inside.any():
            return None
        z = w0 * az + w1 * bz + w2 * cz
        return y0, x0, inside, w0, w1, w2, z

    def write(self, y0: int, x0: int, inside, rgba, z=None, depth_test=False, depth_write=True, blend=False):
        """Writes rgba (h, w, 4 in 0..1) where inside (and the depth test) holds."""
        h, w = inside.shape
        if depth_test and z is not None:
            inside = inside & (z <= self.depth[y0:y0 + h, x0:x0 + w])
        rgba = numpy.clip(rgba, 0, 1)
        target = self.color[y0:y0 + h, x0:x0 + w]
        if blend:
            alpha = rgba[..., 3:4]
            mixed = rgba[..., :3] * 255 * alpha + target[..., :3].astype(numpy.float32) * (1 - alpha)
            target[..., :3][inside] = numpy.clip(mixed + 0.5, 0, 255).astype(numpy.uint8)[inside]
        else:
            target[inside] = numpy.round(rgba * 255).astype(numpy.uint8)[inside]
        if depth_write and depth_test and z is not None:
            self.depth[y0:y0 + h, x0:x0 + w][inside] = z[inside]

    def triangle(self, a: Vertex, b: Vertex, c: Vertex, smooth=True, depth_test=False, depth_write=True, blend=False,
                 cull: int = 0, uvs=None, texture=None, linear=False, address="wrap", inv_w=None):
        """uvs: the texture coordinates of the 3 vertices, texture: an (h, w, 4) array of 0..1 that
        multiplies the color (MODULATE); inv_w: 1/w of the 3 vertices (perspective-correct)."""
        covered = self.cover(a[:3], b[:3], c[:3], cull)
        if covered is None:
            return
        y0, x0, inside, w0, w1, w2, z = covered
        if inv_w is not None:
            p0, p1, p2 = w0 * inv_w[0], w1 * inv_w[1], w2 * inv_w[2]
            total = p0 + p1 + p2
            total[total == 0] = 1
            w0, w1, w2 = p0 / total, p1 / total, p2 / total
        colors = numpy.array([a[3], b[3], c[3]], dtype=numpy.float64)
        if smooth:
            rgba = w0[..., None] * colors[0] + w1[..., None] * colors[1] + w2[..., None] * colors[2]
        else:
            rgba = numpy.broadcast_to(colors[2], w0.shape + (4,)).copy()
        if texture is not None and uvs is not None:
            uv = numpy.array(uvs, dtype=numpy.float64)
            u = w0 * uv[0][0] + w1 * uv[1][0] + w2 * uv[2][0]
            v = w0 * uv[0][1] + w1 * uv[1][1] + w2 * uv[2][1]
            rgba = rgba * sample(texture, u, v, linear, address)
        self.write(y0, x0, inside, rgba, z, depth_test, depth_write, blend)

    def lines(self, points: List[Tuple[float, float]], rgba: Sequence[float], width: float = 1.0, closed=False):
        from PIL import Image, ImageDraw
        image = Image.fromarray(self.color, "RGBA")
        line = list(points) + ([points[0]] if closed and points else [])
        ImageDraw.Draw(image).line(line, fill=tuple(round(max(0, min(1, c)) * 255) for c in rgba), width=max(1, round(width)))
        self.color = numpy.array(image)

    def points(self, points: List[Tuple[float, float]], rgba: Sequence[float], size: float = 1.0):
        from PIL import Image, ImageDraw
        image = Image.fromarray(self.color, "RGBA")
        draw = ImageDraw.Draw(image)
        half = max(0.5, size / 2)
        fill = tuple(round(max(0, min(1, c)) * 255) for c in rgba)
        for x, y in points:
            draw.rectangle([x - half + 0.5, y - half + 0.5, x + half - 0.5, y + half - 0.5], fill=fill)
        self.color = numpy.array(image)

    def image(self):
        from PIL import Image
        return Image.fromarray(self.color, "RGBA").convert("RGB")


def _address(coord, size: int, mode: str):
    if mode == "clamp" or mode == "border":
        return numpy.clip(coord, 0, size - 1)
    if mode == "mirror":
        period = numpy.mod(coord, 2 * size)
        return numpy.where(period < size, period, 2 * size - 1 - period)
    return numpy.mod(coord, size)


def sample(texture, u, v, linear=False, address="wrap"):
    """The texels of texture ((h, w, 4) floats) at the coordinates u, v (0..1, arrays of any shape)."""
    h, w = texture.shape[:2]
    u = numpy.nan_to_num(numpy.asarray(u, dtype=numpy.float64))
    v = numpy.nan_to_num(numpy.asarray(v, dtype=numpy.float64))
    if not linear:
        x = _address(numpy.floor(u * w).astype(numpy.int64), w, address)
        y = _address(numpy.floor(v * h).astype(numpy.int64), h, address)
        return texture[y, x]
    fx, fy = u * w - 0.5, v * h - 0.5
    x0, y0 = numpy.floor(fx).astype(numpy.int64), numpy.floor(fy).astype(numpy.int64)
    tx, ty = (fx - x0)[..., None], (fy - y0)[..., None]
    xa, xb = _address(x0, w, address), _address(x0 + 1, w, address)
    ya, yb = _address(y0, h, address), _address(y0 + 1, h, address)
    top = texture[ya, xa] * (1 - tx) + texture[ya, xb] * tx
    bottom = texture[yb, xa] * (1 - tx) + texture[yb, xb] * tx
    return top * (1 - ty) + bottom * ty


def primitives(kind: str, count: int) -> List[Tuple[int, ...]]:
    """The vertex indices of the triangles (or lines, points) of a primitive of count vertices.
    kind: triangles, strip, fan, quads, quad_strip, polygon, lines, line_strip, line_loop, points."""
    if kind == "triangles":
        return [(i, i + 1, i + 2) for i in range(0, count - 2, 3)]
    if kind == "strip":
        return [(i, i + 1, i + 2) if i % 2 == 0 else (i + 1, i, i + 2) for i in range(count - 2)]
    if kind in ("fan", "polygon"):
        return [(0, i, i + 1) for i in range(1, count - 1)]
    if kind == "quads":
        out = []
        for i in range(0, count - 3, 4):
            out += [(i, i + 1, i + 2), (i, i + 2, i + 3)]
        return out
    if kind == "quad_strip":
        out = []
        for i in range(0, count - 3, 2):
            out += [(i, i + 1, i + 3), (i, i + 3, i + 2)]
        return out
    if kind == "lines":
        return [(i, i + 1) for i in range(0, count - 1, 2)]
    if kind == "line_strip":
        return [(i, i + 1) for i in range(count - 1)]
    if kind == "line_loop":
        return [(i, (i + 1) % count) for i in range(count)] if count > 1 else []
    return [(i,) for i in range(count)]
