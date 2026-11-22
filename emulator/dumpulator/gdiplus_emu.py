# Fathom addition to dumpulator: GDI+ for the emulated program, drawn with Pillow. Boost Software
# License 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# gdiplus.dll draws through the kernel side of GDI (win32k), which the emulator does not have: its
# flat API (the Gdip* functions the C++ classes of gdiplus.h call) is implemented here instead.
# Graphics on an HDC (its surface in gui_draw.py), a window or an image; ARGB colors composited
# with their alpha; world transforms (translate, scale, rotate, a matrix); a rectangular clip;
# solid and linear gradient brushes; pens; rectangles, ellipses, pies, lines, polygons, arcs,
# Béziers and paths, filled and drawn; text (fonts of the Windows fonts folder, alignments,
# DrawString, MeasureString); bitmaps (their own pixels, or the program's memory with Scan0),
# LockBits, pixels, HBITMAP both ways, files (loaded, and saved with the encoders' CLSIDs: PNG,
# BMP, JPEG, GIF, TIFF, into the emulator's files), DrawImage. The other Gdip functions return
# NotImplemented (named in the summary).

import io
import math
import os
import struct
import uuid
from typing import Dict, List, Optional

from .apihooks import read_string, read_wstring
from .graphics_tables import GDIPLUS

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = ImageDraw = None

OK, GENERIC_ERROR, INVALID_PARAMETER, NOT_IMPLEMENTED, FILE_NOT_FOUND = 0, 1, 2, 6, 10
ENCODERS = [  # CLSID, format, name, extension, mime type, Pillow format
    ("557CF400-1A04-11D3-9A73-0000F81EF32E", "B96B3CAB-0728-11D3-9D7B-0000F81EF32E", "BMP", "*.BMP;*.DIB;*.RLE", "image/bmp", "BMP"),
    ("557CF401-1A04-11D3-9A73-0000F81EF32E", "B96B3CAE-0728-11D3-9D7B-0000F81EF32E", "JPEG", "*.JPG;*.JPEG;*.JPE;*.JFIF", "image/jpeg", "JPEG"),
    ("557CF402-1A04-11D3-9A73-0000F81EF32E", "B96B3CB0-0728-11D3-9D7B-0000F81EF32E", "GIF", "*.GIF", "image/gif", "GIF"),
    ("557CF405-1A04-11D3-9A73-0000F81EF32E", "B96B3CB1-0728-11D3-9D7B-0000F81EF32E", "TIFF", "*.TIF;*.TIFF", "image/tiff", "TIFF"),
    ("557CF406-1A04-11D3-9A73-0000F81EF32E", "B96B3CAF-0728-11D3-9D7B-0000F81EF32E", "PNG", "*.PNG", "image/png", "PNG"),
]
FONT_FILES = {"arial": ("arial.ttf", "arialbd.ttf"), "segoe ui": ("segoeui.ttf", "segoeuib.ttf"),
              "times new roman": ("times.ttf", "timesbd.ttf"), "courier new": ("cour.ttf", "courbd.ttf"),
              "tahoma": ("tahoma.ttf", "tahomabd.ttf"), "verdana": ("verdana.ttf", "verdanab.ttf"),
              "consolas": ("consola.ttf", "consolab.ttf"), "calibri": ("calibri.ttf", "calibrib.ttf"),
              "microsoft sans serif": ("micross.ttf", "micross.ttf"), "georgia": ("georgia.ttf", "georgiab.ttf")}


def _argb(value: int):
    value &= 0xFFFFFFFF
    return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF, value >> 24


class Brush:
    def __init__(self, color=0, gradient=None):
        self.color = color          # ARGB
        self.gradient = gradient    # ((x1, y1), (x2, y2), color1, color2)


class Pen:
    def __init__(self, color: int, width: float):
        self.color, self.width = color, max(0.0, width)


class GpImage:
    def __init__(self, image, memory=None):
        self.image = image          # RGBA
        self.memory = memory        # (address, stride, pixel format) of a bitmap made from Scan0
        self.locked = None


class Font:
    def __init__(self, family: str, size: float, style: int):
        self.family, self.size, self.style = family, size, style

    def pil(self):
        from PIL import ImageFont
        files = FONT_FILES.get(self.family.lower(), ("segoeui.ttf", "segoeuib.ttf"))
        name = files[1] if self.style & 1 else files[0]
        try:
            return ImageFont.truetype(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts", name), max(6, round(self.size)))
        except Exception:
            return ImageFont.load_default()


class Graphics:
    def __init__(self, hdc: int = 0, image: Optional[GpImage] = None):
        self.hdc, self.image = hdc, image
        self.matrix = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]   # m11 m12 m21 m22 dx dy
        self.clip = None                              # (x0, y0, x1, y1) in pixels
        self.saved: List[tuple] = []

    def point(self, x: float, y: float):
        m = self.matrix
        return x * m[0] + y * m[2] + m[4], x * m[1] + y * m[3] + m[5]

    def scale(self) -> float:
        m = self.matrix
        return math.sqrt(abs(m[0] * m[3] - m[1] * m[2])) or 1.0

    def axis_aligned(self) -> bool:
        return abs(self.matrix[1]) < 1e-9 and abs(self.matrix[2]) < 1e-9


def _multiply(a, b):
    """The matrix a then b (GDI+ row vectors: [x y 1] * M)."""
    return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]


def _ellipse(x, y, w, h, start=0.0, sweep=360.0, steps=72):
    cx, cy, rx, ry = x + w / 2, y + h / 2, w / 2, h / 2
    count = max(2, int(steps * abs(sweep) / 360))
    return [(cx + rx * math.cos(math.radians(start + sweep * i / count)), cy + ry * math.sin(math.radians(start + sweep * i / count)))
            for i in range(count + 1)]


def _bezier(points):
    out = [points[0]]
    for i in range(0, len(points) - 3, 3):
        p0, p1, p2, p3 = points[i:i + 4]
        for step in range(1, 17):
            t = step / 16
            u = 1 - t
            out.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                        u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
    return out


class GdiPlus:
    def __init__(self, dp):
        self.dp = dp
        self.objects: Dict[int, object] = {}
        self.events: List[dict] = []

    # ------------------------------------------------------------------ helpers
    def new(self, obj, out: int) -> int:
        address = self.dp.allocate(16)
        self.objects[address] = obj
        if out:
            self.dp.write_ptr(out, address)
        return OK

    def get(self, handle: int, kind=None):
        obj = self.objects.get(handle)
        return obj if obj is None or kind is None or isinstance(obj, kind) else None

    def delete(self, handle: int):
        self.objects.pop(handle, None)
        return OK

    def _floats(self, address: int, count: int) -> list:
        return list(struct.unpack(f"<{count}f", bytes(self.dp.read(address, 4 * count)))) if count else []

    def _ints(self, address: int, count: int) -> list:
        return list(struct.unpack(f"<{count}i", bytes(self.dp.read(address, 4 * count)))) if count else []

    def _points(self, address: int, count: int, integer: bool) -> list:
        values = self._ints(address, 2 * count) if integer else self._floats(address, 2 * count)
        return [(values[2 * i], values[2 * i + 1]) for i in range(count)]

    def _sync_in(self, image: GpImage):
        if image.memory is not None:
            address, stride, fmt = image.memory
            width, height = image.image.size
            data = bytes(self.dp.read(address, stride * height))
            image.image = _decode(data, width, height, stride, fmt)

    def _sync_out(self, image: GpImage):
        if image.memory is not None:
            address, stride, fmt = image.memory
            self.dp.write(address, _encode(image.image, stride, fmt))

    def render(self, g: Graphics, function):
        """function(draw, layer) on a transparent layer, composited onto the target through the clip."""
        if Image is None:
            return

        def apply(image):
            layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
            function(ImageDraw.Draw(layer), layer)
            if g.clip is not None:
                mask = Image.new("L", image.size, 0)
                ImageDraw.Draw(mask).rectangle([g.clip[0], g.clip[1], g.clip[2] - 1, g.clip[3] - 1], fill=255)
                alpha = layer.getchannel("A")
                from PIL import ImageChops
                layer.putalpha(ImageChops.multiply(alpha, mask))
            if image.mode == "RGBA":
                image.alpha_composite(layer)
            else:
                image.paste(layer.convert("RGB"), (0, 0), layer.getchannel("A"))

        if g.image is not None:
            self._sync_in(g.image)
            apply(g.image.image)
            self._sync_out(g.image)
        elif g.hdc:
            drawing = self.dp.gui.drawing
            drawing._paint(g.hdc, lambda draw, dc, image: apply(image))

    def _fill(self, g: Graphics, brush: Brush, polygon):
        if brush is None or len(polygon) < 3:
            return INVALID_PARAMETER
        points = [g.point(x, y) for x, y in polygon]

        def draw(d, layer):
            if brush.gradient is None:
                d.polygon(points, fill=_argb(brush.color))
                return
            mask = Image.new("L", layer.size, 0)
            ImageDraw.Draw(mask).polygon(points, fill=255)
            layer.paste(_gradient(layer.size, brush.gradient, g), (0, 0), mask)
        self.render(g, draw)
        return OK

    def _rect_polygon(self, g, x, y, w, h):
        if g.axis_aligned():  # pixel coverage of [x, x + w)
            return [(x, y), (x + w - 1, y), (x + w - 1, y + h - 1), (x, y + h - 1)]
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]

    def _stroke(self, g: Graphics, pen: Pen, points, closed=False):
        if pen is None or len(points) < 2:
            return INVALID_PARAMETER
        line = [g.point(x, y) for x, y in points]
        if closed:
            line.append(line[0])
        width = max(1, round(pen.width * g.scale()))
        self.render(g, lambda d, layer: d.line(line, fill=_argb(pen.color), width=width, joint="curve"))
        return OK

    # ------------------------------------------------------------------ start-up, graphics
    def startup(self, dp, token, input_, output):
        if token:
            dp.write_ptr(token, 0x1000)
        if output:
            dp.write(output, bytes(2 * dp.ptr_size()))
        self.events.append(dict(kind="gdiplus_startup"))
        return OK

    def from_hdc(self, dp, hdc, out):
        return self.new(Graphics(hdc=hdc), out)

    def from_hwnd(self, dp, hwnd, out):
        return self.new(Graphics(hdc=dp.gui.drawing.new_dc(hwnd)), out)

    def image_graphics(self, dp, image, out):
        img = self.get(image, GpImage)
        return self.new(Graphics(image=img), out) if img is not None else INVALID_PARAMETER

    def clear(self, dp, graphics, color):
        g = self.get(graphics, Graphics)
        if g is None:
            return INVALID_PARAMETER

        def draw(d, layer):
            d.rectangle([0, 0, layer.size[0], layer.size[1]], fill=_argb(color))
        if g.image is not None:  # Clear replaces the pixels (with their alpha)
            self._sync_in(g.image)
            g.image.image.paste(_argb(color), [0, 0, g.image.image.size[0], g.image.image.size[1]])
            self._sync_out(g.image)
            return OK
        self.render(g, draw)
        return OK

    def get_dc(self, dp, graphics, out):
        g = self.get(graphics, Graphics)
        if g is None or not g.hdc:
            return INVALID_PARAMETER
        dp.write_ptr(out, g.hdc)
        return OK

    def transform(self, dp, graphics, kind, a=0.0, b=0.0, order=0):
        g = self.get(graphics, Graphics)
        if g is None:
            return INVALID_PARAMETER
        if kind == "translate":
            m = [1, 0, 0, 1, a, b]
        elif kind == "scale":
            m = [a, 0, 0, b, 0, 0]
        elif kind == "rotate":
            r = math.radians(a)
            m = [math.cos(r), math.sin(r), -math.sin(r), math.cos(r), 0, 0]
            order = b  # RotateWorldTransform(graphics, angle, order)
        else:
            g.matrix = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
            return OK
        # MatrixOrderPrepend (0): the new transformation first
        g.matrix = _multiply(m, g.matrix) if int(order) == 0 else _multiply(g.matrix, m)
        return OK

    def set_world(self, dp, graphics, matrix):
        g, m = self.get(graphics, Graphics), self.get(matrix, list)
        if g is None or m is None:
            return INVALID_PARAMETER
        g.matrix = list(m)
        return OK

    def clip_rect(self, dp, graphics, x, y, w, h, mode):
        g = self.get(graphics, Graphics)
        if g is None:
            return INVALID_PARAMETER
        corners = [g.point(x, y), g.point(x + w, y + h)]
        box = (min(c[0] for c in corners), min(c[1] for c in corners), max(c[0] for c in corners), max(c[1] for c in corners))
        if int(mode) == 1 and g.clip is not None:  # CombineModeIntersect
            box = (max(box[0], g.clip[0]), max(box[1], g.clip[1]), min(box[2], g.clip[2]), min(box[3], g.clip[3]))
        g.clip = box
        return OK

    def save(self, dp, graphics, out):
        g = self.get(graphics, Graphics)
        if g is None:
            return INVALID_PARAMETER
        g.saved.append((list(g.matrix), g.clip))
        dp.write_ulong(out, len(g.saved))
        return OK

    def restore(self, dp, graphics, state):
        g = self.get(graphics, Graphics)
        if g is None or not 0 < state <= len(g.saved):
            return INVALID_PARAMETER
        g.matrix, g.clip = g.saved[state - 1]
        del g.saved[state - 1:]
        return OK

    # ------------------------------------------------------------------ brushes, pens, matrices
    def solid(self, dp, color, out):
        return self.new(Brush(color), out)

    def line_brush(self, dp, p1, p2, c1, c2, wrap, out, integer):
        (a, b) = self._points(p1, 1, integer)[0], self._points(p2, 1, integer)[0]
        return self.new(Brush(c1, (a, b, c1, c2)), out)

    def line_brush_rect(self, dp, rect, c1, c2, mode, wrap, out, integer):
        x, y, w, h = self._ints(rect, 4) if integer else self._floats(rect, 4)
        # LinearGradientModeHorizontal 0, Vertical 1, ForwardDiagonal 2, BackwardDiagonal 3
        ends = {0: ((x, y), (x + w, y)), 1: ((x, y), (x, y + h)), 2: ((x, y), (x + w, y + h)), 3: ((x + w, y), (x, y + h))}[int(mode) & 3]
        return self.new(Brush(c1, (ends[0], ends[1], c1, c2)), out)

    def pen(self, dp, color, width, unit, out):
        return self.new(Pen(color, width), out)

    def pen_from_brush(self, dp, brush, width, unit, out):
        b = self.get(brush, Brush)
        return self.new(Pen(b.color if b else 0xFF000000, width), out)

    def matrix(self, dp, m11, m12, m21, m22, dx, dy, out):
        return self.new([m11, m12, m21, m22, dx, dy], out)

    # ------------------------------------------------------------------ shapes
    def fill_rect(self, dp, graphics, brush, x, y, w, h):
        g = self.get(graphics, Graphics)
        return self._fill(g, self.get(brush, Brush), self._rect_polygon(g, x, y, w, h)) if g else INVALID_PARAMETER

    def fill_rects(self, dp, graphics, brush, rects, count, integer):
        g = self.get(graphics, Graphics)
        values = self._ints(rects, 4 * count) if integer else self._floats(rects, 4 * count)
        for i in range(count):
            self._fill(g, self.get(brush, Brush), self._rect_polygon(g, *values[4 * i:4 * i + 4]))
        return OK

    def fill_ellipse(self, dp, graphics, brush, x, y, w, h):
        g = self.get(graphics, Graphics)
        return self._fill(g, self.get(brush, Brush), _ellipse(x, y, w - 1, h - 1)) if g else INVALID_PARAMETER

    def fill_pie(self, dp, graphics, brush, x, y, w, h, start, sweep):
        g = self.get(graphics, Graphics)
        points = [(x + w / 2, y + h / 2)] + _ellipse(x, y, w, h, start, sweep)
        return self._fill(g, self.get(brush, Brush), points) if g else INVALID_PARAMETER

    def fill_polygon(self, dp, graphics, brush, points, count, mode, integer):
        g = self.get(graphics, Graphics)
        return self._fill(g, self.get(brush, Brush), self._points(points, count, integer)) if g else INVALID_PARAMETER

    def draw_line(self, dp, graphics, pen, x1, y1, x2, y2):
        return self._stroke(self.get(graphics, Graphics), self.get(pen, Pen), [(x1, y1), (x2, y2)])

    def draw_lines(self, dp, graphics, pen, points, count, integer, closed=False):
        return self._stroke(self.get(graphics, Graphics), self.get(pen, Pen), self._points(points, count, integer), closed)

    def draw_rect(self, dp, graphics, pen, x, y, w, h):
        return self._stroke(self.get(graphics, Graphics), self.get(pen, Pen), [(x, y), (x + w, y), (x + w, y + h), (x, y + h)], True)

    def draw_rects(self, dp, graphics, pen, rects, count, integer):
        values = self._ints(rects, 4 * count) if integer else self._floats(rects, 4 * count)
        for i in range(count):
            self.draw_rect(dp, graphics, pen, *values[4 * i:4 * i + 4])
        return OK

    def draw_ellipse(self, dp, graphics, pen, x, y, w, h):
        return self._stroke(self.get(graphics, Graphics), self.get(pen, Pen), _ellipse(x, y, w, h))

    def draw_arc(self, dp, graphics, pen, x, y, w, h, start, sweep, pie=False):
        points = _ellipse(x, y, w, h, start, sweep)
        if pie:
            points = [(x + w / 2, y + h / 2)] + points
        return self._stroke(self.get(graphics, Graphics), self.get(pen, Pen), points, pie)

    def draw_bezier(self, dp, graphics, pen, *values):
        points = [(values[i], values[i + 1]) for i in range(0, 8, 2)]
        return self._stroke(self.get(graphics, Graphics), self.get(pen, Pen), _bezier(points))

    def draw_beziers(self, dp, graphics, pen, points, count, integer):
        return self._stroke(self.get(graphics, Graphics), self.get(pen, Pen), _bezier(self._points(points, count, integer)))

    # ------------------------------------------------------------------ paths
    def path(self, dp, mode, out):
        return self.new({"figures": [[]], "closed": [False], "mode": mode}, out)

    def _figure(self, path):
        return path["figures"][-1]

    def path_add(self, dp, path, points):
        p = self.get(path, dict)
        if p is None:
            return INVALID_PARAMETER
        self._figure(p).extend(points)
        return OK

    def path_shape(self, dp, path, points):
        p = self.get(path, dict)
        if p is None:
            return INVALID_PARAMETER
        if self._figure(p):
            p["figures"].append([])
            p["closed"].append(False)
        self._figure(p).extend(points)
        p["closed"][-1] = True
        p["figures"].append([])
        p["closed"].append(False)
        return OK

    def path_close(self, dp, path, start_new=True):
        p = self.get(path, dict)
        if p is None:
            return INVALID_PARAMETER
        if self._figure(p):
            p["closed"][-1] = True
            p["figures"].append([])
            p["closed"].append(False)
        return OK

    def fill_path(self, dp, graphics, brush, path):
        g, b, p = self.get(graphics, Graphics), self.get(brush, Brush), self.get(path, dict)
        if g is None or b is None or p is None:
            return INVALID_PARAMETER
        for figure in p["figures"]:
            if len(figure) >= 3:
                self._fill(g, b, figure)
        return OK

    def draw_path(self, dp, graphics, pen, path):
        g, pn, p = self.get(graphics, Graphics), self.get(pen, Pen), self.get(path, dict)
        if g is None or pn is None or p is None:
            return INVALID_PARAMETER
        for figure, closed in zip(p["figures"], p["closed"]):
            if len(figure) >= 2:
                self._stroke(g, pn, figure, closed)
        return OK

    # ------------------------------------------------------------------ text
    def family(self, dp, name, collection, out):
        return self.new(read_wstring(dp, name) or "Segoe UI", out)

    def generic_family(self, dp, out, name):
        return self.new(name, out)

    def font(self, dp, family, size, style, unit, out):
        name = self.get(family, str) or "Segoe UI"
        pixels = size * 96 / 72 if int(unit) in (3, 0) else size  # UnitPoint, UnitWorld (points by default)
        if int(unit) == 2:  # UnitPixel
            pixels = size
        return self.new(Font(name, pixels, style), out)

    def font_from_dc(self, dp, hdc, out):
        dc = dp.gui.drawing.dcs.get(hdc)
        return self.new(Font("Segoe UI", dc.font.size if dc else 15, 0), out)

    def font_from_logfont(self, dp, hdc, logfont, out, wide):
        height = struct.unpack("<i", bytes(dp.read(logfont, 4)))[0]
        weight = dp.read_ulong(logfont + 16)
        name = read_string(dp, logfont + 28, wide) or "Segoe UI"
        return self.new(Font(name, abs(height) or 15, 1 if weight >= 700 else 0), out)

    def string_format(self, dp, flags, language, out):
        return self.new({"align": 0, "line": 0}, out)

    def generic_format(self, dp, out):
        return self.new({"align": 0, "line": 0}, out)

    def format_align(self, dp, fmt, align, key):
        f = self.get(fmt, dict)
        if f is None:
            return INVALID_PARAMETER
        f[key] = int(align)
        return OK

    def _layout(self, g, text, font: Font, rect, fmt):
        face = font.pil()
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        box = probe.textbbox((0, 0), text, font=face)
        width, height = box[2] - box[0], box[3] - box[1]
        x, y, w, h = rect
        align = fmt.get("align", 0) if fmt else 0
        line = fmt.get("line", 0) if fmt else 0
        if w > 0:
            x += {1: (w - width) / 2, 2: w - width}.get(align, 0)
        if h > 0:
            y += {1: (h - height) / 2, 2: h - height}.get(line, 0)
        return x, y, width, height, face

    def draw_string(self, dp, graphics, text, length, font, rect, fmt, brush):
        g, f, b = self.get(graphics, Graphics), self.get(font, Font), self.get(brush, Brush)
        if g is None or f is None or b is None or Image is None:
            return INVALID_PARAMETER
        length = struct.unpack("<i", struct.pack("<I", length & 0xFFFFFFFF))[0]
        value = read_wstring(dp, text) if length < 0 else bytes(dp.read(text, 2 * length)).decode("utf-16-le", "replace")
        x, y, width, height, face = self._layout(g, value, f, self._floats(rect, 4), self.get(fmt, dict))
        origin = g.point(x, y)
        self.events.append(dict(kind="draw_string", text=value))
        self.render(g, lambda d, layer: d.text(origin, value, fill=_argb(b.color), font=face))
        return OK

    def measure_string(self, dp, graphics, text, length, font, rect, fmt, out, characters, lines):
        g, f = self.get(graphics, Graphics), self.get(font, Font)
        if f is None or Image is None:
            return INVALID_PARAMETER
        length = struct.unpack("<i", struct.pack("<I", length & 0xFFFFFFFF))[0]
        value = read_wstring(dp, text) if length < 0 else bytes(dp.read(text, 2 * length)).decode("utf-16-le", "replace")
        x, y, width, height, face = self._layout(g, value, f, self._floats(rect, 4), self.get(fmt, dict))
        dp.write(out, struct.pack("<ffff", x, y, width, height))
        if characters:
            dp.write_ulong(characters, len(value))
        if lines:
            dp.write_ulong(lines, value.count("\n") + 1)
        return OK

    # ------------------------------------------------------------------ images
    def bitmap_scan0(self, dp, width, height, stride, fmt, scan0, out):
        if Image is None:
            return NOT_IMPLEMENTED
        width, height = max(1, width), max(1, height)
        if scan0:
            stride = struct.unpack("<i", struct.pack("<I", stride & 0xFFFFFFFF))[0]
            image = GpImage(Image.new("RGBA", (width, height)), (scan0, stride, fmt))
            self._sync_in(image)
            return self.new(image, out)
        return self.new(GpImage(Image.new("RGBA", (width, height), (0, 0, 0, 0))), out)

    def bitmap_from_graphics(self, dp, width, height, graphics, out):
        return self.bitmap_scan0(dp, width, height, 0, 0x26200A, 0, out)

    def bitmap_from_hbitmap(self, dp, hbitmap, palette, out):
        drawing = dp.gui.drawing
        obj = drawing.object(hbitmap)
        if obj is None or obj.kind != "bitmap" or Image is None:
            return INVALID_PARAMETER
        return self.new(GpImage(obj.surface(dp).convert("RGBA")), out)

    def hbitmap_from_bitmap(self, dp, bitmap, out, background):
        image = self.get(bitmap, GpImage)
        if image is None:
            return INVALID_PARAMETER
        self._sync_in(image)
        base = Image.new("RGBA", image.image.size, _argb(background))
        base.alpha_composite(image.image)
        dp.write_ptr(out, dp.gui.drawing.new_bitmap(*image.image.size, base.convert("RGB")))
        return OK

    def load_file(self, dp, name, out):
        from .filesystem import filesystem
        path = read_wstring(dp, name) or ""
        try:
            data = filesystem(dp).read(path)
            return self.new(GpImage(Image.open(io.BytesIO(data)).convert("RGBA")), out)
        except Exception:
            return FILE_NOT_FOUND

    def save_file(self, dp, image, name, clsid, params):
        img = self.get(image, GpImage)
        if img is None:
            return INVALID_PARAMETER
        self._sync_in(img)
        from .apihooks import read_guid
        encoder = read_guid(dp, clsid) if clsid else ""
        fmt = next((e[5] for e in ENCODERS if e[0] == encoder), "PNG")
        out = io.BytesIO()
        picture = img.image if fmt in ("PNG", "TIFF", "GIF") else img.image.convert("RGB")
        picture.save(out, format=fmt)
        path = read_wstring(dp, name) or ""
        from .filesystem import filesystem
        fs = filesystem(dp)
        state = fs.materialize(path) if fs.exists(path) else fs.create(path, False)
        state.data = bytearray(out.getvalue())
        state.written = True
        self.events.append(dict(kind="save_image", path=path, format=fmt, size=len(state.data)))
        return OK

    def encoders_size(self, dp, count, size):
        ptr = dp.ptr_size()
        record = 104 if ptr == 8 else 76
        dp.write_ulong(count, len(ENCODERS))
        dp.write_ulong(size, len(ENCODERS) * (record + 256))
        return OK

    def encoders(self, dp, count, size, out):
        """ImageCodecInfo[] {Clsid, FormatID, CodecName, DllName, FormatDescription, FilenameExtension,
        MimeType, Flags, Version, SigCount, SigSize, SigPattern, SigMask}, the strings after them."""
        ptr = dp.ptr_size()
        record = 104 if ptr == 8 else 76
        strings = out + len(ENCODERS) * record
        pack = "<Q" if ptr == 8 else "<I"
        for i, (clsid, fmt, name, extension, mime, _) in enumerate(ENCODERS):
            texts = []
            for text in (f"Built-in {name} Codec", "", name, extension, mime):
                data = (text + "\0").encode("utf-16-le")
                dp.write(strings, data)
                texts.append(strings)
                strings += len(data)
            entry = uuid.UUID(clsid).bytes_le + uuid.UUID(fmt).bytes_le + b"".join(struct.pack(pack, t) for t in texts)
            entry += struct.pack("<IIII", 0x10007, 1, 0, 0) + struct.pack(pack, 0) + struct.pack(pack, 0)
            dp.write(out + i * record, entry)
        return OK

    def dispose(self, dp, image):
        return self.delete(image)

    def image_size(self, dp, image, out, index):
        img = self.get(image, GpImage)
        if img is None:
            return INVALID_PARAMETER
        dp.write_ulong(out, img.image.size[index])
        return OK

    def pixel(self, dp, bitmap, x, y, out=None, color=None):
        img = self.get(bitmap, GpImage)
        if img is None or not (0 <= x < img.image.size[0] and 0 <= y < img.image.size[1]):
            return INVALID_PARAMETER
        self._sync_in(img)
        if color is None:
            r, g, b, a = img.image.getpixel((x, y))
            dp.write_ulong(out, (a << 24) | (r << 16) | (g << 8) | b)
        else:
            img.image.putpixel((x, y), _argb(color))
            self._sync_out(img)
        return OK

    def lock_bits(self, dp, bitmap, rect, flags, fmt, data):
        """BitmapData {Width, Height, Stride, PixelFormat, Scan0, Reserved}: the pixels copied into the
        program's memory (in fmt), back into the image by UnlockBits when written."""
        img = self.get(bitmap, GpImage)
        if img is None:
            return INVALID_PARAMETER
        self._sync_in(img)
        x, y, w, h = self._ints(rect, 4) if rect else (0, 0) + img.image.size
        bpp = 3 if (fmt & 0xFF00) >> 8 == 24 else 4
        stride = (w * bpp + 3) & ~3
        address = dp.allocate((stride * h + 0xFFF) & ~0xFFF or 0x1000, page_align=True)
        part = img.image.crop((x, y, x + w, y + h))
        dp.write(address, _encode(part, stride, fmt))
        ptr = dp.ptr_size()
        dp.write(data, struct.pack("<IIiI", w, h, stride, fmt) + struct.pack("<Q" if ptr == 8 else "<I", address) + bytes(ptr))
        img.locked = (x, y, w, h, stride, fmt, address, flags)
        return OK

    def unlock_bits(self, dp, bitmap, data):
        img = self.get(bitmap, GpImage)
        if img is None or img.locked is None:
            return INVALID_PARAMETER
        x, y, w, h, stride, fmt, address, flags = img.locked
        if flags & 2:  # ImageLockModeWrite
            part = _decode(bytes(dp.read(address, stride * h)), w, h, stride, fmt)
            img.image.paste(part, (x, y))
            self._sync_out(img)
        img.locked = None
        return OK

    def draw_image(self, dp, graphics, image, x, y, w=None, h=None, src=None):
        g, img = self.get(graphics, Graphics), self.get(image, GpImage)
        if g is None or img is None or Image is None:
            return INVALID_PARAMETER
        self._sync_in(img)
        picture = img.image
        if src is not None:
            sx, sy, sw, sh = src
            picture = picture.crop((round(sx), round(sy), round(sx + sw), round(sy + sh)))
        if w is None:
            w, h = picture.size
        corners = [g.point(x, y), g.point(x + w, y + h)]
        left, top = round(min(c[0] for c in corners)), round(min(c[1] for c in corners))
        size = (max(1, round(abs(corners[1][0] - corners[0][0]))), max(1, round(abs(corners[1][1] - corners[0][1]))))
        scaled = picture.resize(size) if size != picture.size else picture
        self.render(g, lambda d, layer: layer.alpha_composite(scaled, (left, top)) if 0 <= left < layer.size[0] and 0 <= top < layer.size[1]
                    else layer.paste(scaled, (left, top), scaled))
        return OK


def _decode(data: bytes, width: int, height: int, stride: int, fmt: int):
    """Pixels of a GDI+ pixel format into RGBA (a negative stride: bottom-up)."""
    orientation = -1 if stride < 0 else 1
    stride = abs(stride)
    if fmt in (0x26200A, 0xE200B):   # 32bppARGB, 32bppPARGB
        return Image.frombuffer("RGBA", (width, height), data, "raw", "BGRA", stride, orientation)
    if fmt == 0x22009:               # 32bppRGB
        return Image.frombuffer("RGB", (width, height), data, "raw", "BGRX", stride, orientation).convert("RGBA")
    if fmt == 0x21808:               # 24bppRGB
        return Image.frombuffer("RGB", (width, height), data, "raw", "BGR", stride, orientation).convert("RGBA")
    return Image.frombuffer("RGBA", (width, height), data, "raw", "BGRA", stride, orientation)


def _encode(image, stride: int, fmt: int) -> bytes:
    orientation = -1 if stride < 0 else 1
    stride = abs(stride)
    rgba = image.convert("RGBA")
    if fmt == 0x22009:
        return rgba.convert("RGB").tobytes("raw", "BGRX", stride, orientation)
    if fmt == 0x21808:
        return rgba.convert("RGB").tobytes("raw", "BGR", stride, orientation)
    return rgba.tobytes("raw", "BGRA", stride, orientation)


def _gradient(size, gradient, g: Graphics):
    """A linear gradient between two points (in the graphics' coordinates) over an image of size."""
    (x1, y1), (x2, y2), c1, c2 = gradient
    try:
        import numpy
    except ImportError:  # without numpy: the first color
        return Image.new("RGBA", size, tuple(_argb(c1)))
    p1, p2 = g.point(x1, y1), g.point(x2, y2)
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    length = dx * dx + dy * dy or 1.0
    ys, xs = numpy.mgrid[0:size[1], 0:size[0]].astype(numpy.float32)
    t = numpy.clip(((xs - p1[0]) * dx + (ys - p1[1]) * dy) / length, 0, 1)[..., None]
    a, b = numpy.array(_argb(c1), dtype=numpy.float32), numpy.array(_argb(c2), dtype=numpy.float32)
    return Image.fromarray((a + (b - a) * t + 0.5).astype(numpy.uint8), "RGBA")


def install(dp):
    gp = GdiPlus(dp)
    dp.gdiplus = gp
    hooks = dp.apihooks
    m = "gdiplus.dll"
    implemented = {
        "GdiplusStartup": gp.startup,
        "GdiplusShutdown": lambda dp, token: 0,
        "GdipAlloc": lambda dp, size: dp.allocate(max(1, size)),
        "GdipFree": lambda dp, ptr: 0,
        "GdipCreateFromHDC": gp.from_hdc,
        "GdipCreateFromHDC2": lambda dp, hdc, device, out: gp.from_hdc(dp, hdc, out),
        "GdipCreateFromHWND": gp.from_hwnd,
        "GdipCreateFromHWNDICM": gp.from_hwnd,
        "GdipGetImageGraphicsContext": gp.image_graphics,
        "GdipDeleteGraphics": lambda dp, g: gp.delete(g),
        "GdipGraphicsClear": gp.clear,
        "GdipFlush": lambda dp, g, intention: OK,
        "GdipGetDC": gp.get_dc,
        "GdipReleaseDC": lambda dp, g, hdc: OK,
        "GdipTranslateWorldTransform": lambda dp, g, x, y, order: gp.transform(dp, g, "translate", x, y, order),
        "GdipScaleWorldTransform": lambda dp, g, x, y, order: gp.transform(dp, g, "scale", x, y, order),
        "GdipRotateWorldTransform": lambda dp, g, angle, order: gp.transform(dp, g, "rotate", angle, order),
        "GdipResetWorldTransform": lambda dp, g: gp.transform(dp, g, "reset"),
        "GdipSetWorldTransform": gp.set_world,
        "GdipCreateMatrix": lambda dp, out: gp.new([1.0, 0.0, 0.0, 1.0, 0.0, 0.0], out),
        "GdipCreateMatrix2": gp.matrix,
        "GdipDeleteMatrix": lambda dp, mtx: gp.delete(mtx),
        "GdipSetClipRect": gp.clip_rect,
        "GdipSetClipRectI": gp.clip_rect,
        "GdipResetClip": lambda dp, g: (setattr(gp.get(g, Graphics), "clip", None) if gp.get(g, Graphics) else None, OK)[1],
        "GdipSaveGraphics": gp.save,
        "GdipRestoreGraphics": gp.restore,
        "GdipGetDpiX": lambda dp, g, out: (dp.write(out, struct.pack("<f", 96.0)), OK)[1],
        "GdipGetDpiY": lambda dp, g, out: (dp.write(out, struct.pack("<f", 96.0)), OK)[1],
        "GdipCreateSolidFill": gp.solid,
        "GdipGetSolidFillColor": lambda dp, b, out: (dp.write_ulong(out, gp.get(b, Brush).color & 0xFFFFFFFF), OK)[1] if gp.get(b, Brush) else INVALID_PARAMETER,
        "GdipSetSolidFillColor": lambda dp, b, c: (setattr(gp.get(b, Brush), "color", c), OK)[1] if gp.get(b, Brush) else INVALID_PARAMETER,
        "GdipCreateLineBrush": lambda dp, p1, p2, c1, c2, wrap, out: gp.line_brush(dp, p1, p2, c1, c2, wrap, out, False),
        "GdipCreateLineBrushI": lambda dp, p1, p2, c1, c2, wrap, out: gp.line_brush(dp, p1, p2, c1, c2, wrap, out, True),
        "GdipCreateLineBrushFromRect": lambda dp, r, c1, c2, mode, wrap, out: gp.line_brush_rect(dp, r, c1, c2, mode, wrap, out, False),
        "GdipCreateLineBrushFromRectI": lambda dp, r, c1, c2, mode, wrap, out: gp.line_brush_rect(dp, r, c1, c2, mode, wrap, out, True),
        "GdipCreateHatchBrush": lambda dp, style, fore, back, out: gp.solid(dp, fore, out),
        "GdipDeleteBrush": lambda dp, b: gp.delete(b),
        "GdipCloneBrush": lambda dp, b, out: gp.new(gp.get(b, Brush), out),
        "GdipCreatePen1": gp.pen,
        "GdipCreatePen2": gp.pen_from_brush,
        "GdipDeletePen": lambda dp, p: gp.delete(p),
        "GdipSetPenWidth": lambda dp, p, w: (setattr(gp.get(p, Pen), "width", w), OK)[1] if gp.get(p, Pen) else INVALID_PARAMETER,
        "GdipSetPenColor": lambda dp, p, c: (setattr(gp.get(p, Pen), "color", c), OK)[1] if gp.get(p, Pen) else INVALID_PARAMETER,
        "GdipFillRectangle": gp.fill_rect,
        "GdipFillRectangleI": gp.fill_rect,
        "GdipFillRectangles": lambda dp, g, b, r, n: gp.fill_rects(dp, g, b, r, n, False),
        "GdipFillRectanglesI": lambda dp, g, b, r, n: gp.fill_rects(dp, g, b, r, n, True),
        "GdipFillEllipse": gp.fill_ellipse,
        "GdipFillEllipseI": gp.fill_ellipse,
        "GdipFillPie": gp.fill_pie,
        "GdipFillPieI": gp.fill_pie,
        "GdipFillPolygon": lambda dp, g, b, p, n, mode: gp.fill_polygon(dp, g, b, p, n, mode, False),
        "GdipFillPolygonI": lambda dp, g, b, p, n, mode: gp.fill_polygon(dp, g, b, p, n, mode, True),
        "GdipFillPolygon2": lambda dp, g, b, p, n: gp.fill_polygon(dp, g, b, p, n, 0, False),
        "GdipFillPolygon2I": lambda dp, g, b, p, n: gp.fill_polygon(dp, g, b, p, n, 0, True),
        "GdipDrawLine": gp.draw_line,
        "GdipDrawLineI": gp.draw_line,
        "GdipDrawLines": lambda dp, g, pen, p, n: gp.draw_lines(dp, g, pen, p, n, False),
        "GdipDrawLinesI": lambda dp, g, pen, p, n: gp.draw_lines(dp, g, pen, p, n, True),
        "GdipDrawPolygon": lambda dp, g, pen, p, n: gp.draw_lines(dp, g, pen, p, n, False, True),
        "GdipDrawPolygonI": lambda dp, g, pen, p, n: gp.draw_lines(dp, g, pen, p, n, True, True),
        "GdipDrawRectangle": gp.draw_rect,
        "GdipDrawRectangleI": gp.draw_rect,
        "GdipDrawRectangles": lambda dp, g, pen, r, n: gp.draw_rects(dp, g, pen, r, n, False),
        "GdipDrawRectanglesI": lambda dp, g, pen, r, n: gp.draw_rects(dp, g, pen, r, n, True),
        "GdipDrawEllipse": gp.draw_ellipse,
        "GdipDrawEllipseI": gp.draw_ellipse,
        "GdipDrawArc": gp.draw_arc,
        "GdipDrawArcI": gp.draw_arc,
        "GdipDrawPie": lambda dp, g, pen, x, y, w, h, s, sw: gp.draw_arc(dp, g, pen, x, y, w, h, s, sw, True),
        "GdipDrawPieI": lambda dp, g, pen, x, y, w, h, s, sw: gp.draw_arc(dp, g, pen, x, y, w, h, s, sw, True),
        "GdipDrawBezier": gp.draw_bezier,
        "GdipDrawBezierI": gp.draw_bezier,
        "GdipDrawBeziers": lambda dp, g, pen, p, n: gp.draw_beziers(dp, g, pen, p, n, False),
        "GdipDrawBeziersI": lambda dp, g, pen, p, n: gp.draw_beziers(dp, g, pen, p, n, True),
        "GdipDrawCurve": lambda dp, g, pen, p, n: gp.draw_lines(dp, g, pen, p, n, False),
        "GdipDrawCurveI": lambda dp, g, pen, p, n: gp.draw_lines(dp, g, pen, p, n, True),
        "GdipCreatePath": gp.path,
        "GdipDeletePath": lambda dp, p: gp.delete(p),
        "GdipResetPath": lambda dp, p: (gp.objects.update({p: {"figures": [[]], "closed": [False], "mode": 0}}), OK)[1],
        "GdipSetPathFillMode": lambda dp, p, mode: OK,
        "GdipAddPathLine": lambda dp, p, x1, y1, x2, y2: gp.path_add(dp, p, [(x1, y1), (x2, y2)]),
        "GdipAddPathLineI": lambda dp, p, x1, y1, x2, y2: gp.path_add(dp, p, [(x1, y1), (x2, y2)]),
        "GdipAddPathLine2": lambda dp, p, pts, n: gp.path_add(dp, p, gp._points(pts, n, False)),
        "GdipAddPathLine2I": lambda dp, p, pts, n: gp.path_add(dp, p, gp._points(pts, n, True)),
        "GdipAddPathRectangle": lambda dp, p, x, y, w, h: gp.path_shape(dp, p, [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]),
        "GdipAddPathRectangleI": lambda dp, p, x, y, w, h: gp.path_shape(dp, p, [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]),
        "GdipAddPathEllipse": lambda dp, p, x, y, w, h: gp.path_shape(dp, p, _ellipse(x, y, w, h)),
        "GdipAddPathEllipseI": lambda dp, p, x, y, w, h: gp.path_shape(dp, p, _ellipse(x, y, w, h)),
        "GdipAddPathPolygon": lambda dp, p, pts, n: gp.path_shape(dp, p, gp._points(pts, n, False)),
        "GdipAddPathPolygonI": lambda dp, p, pts, n: gp.path_shape(dp, p, gp._points(pts, n, True)),
        "GdipAddPathArc": lambda dp, p, x, y, w, h, s, sw: gp.path_add(dp, p, _ellipse(x, y, w, h, s, sw)),
        "GdipAddPathArcI": lambda dp, p, x, y, w, h, s, sw: gp.path_add(dp, p, _ellipse(x, y, w, h, s, sw)),
        "GdipAddPathBezier": lambda dp, p, *v: gp.path_add(dp, p, _bezier([(v[i], v[i + 1]) for i in range(0, 8, 2)])),
        "GdipAddPathBezierI": lambda dp, p, *v: gp.path_add(dp, p, _bezier([(v[i], v[i + 1]) for i in range(0, 8, 2)])),
        "GdipClosePathFigure": lambda dp, p: gp.path_close(dp, p),
        "GdipClosePathFigures": lambda dp, p: gp.path_close(dp, p),
        "GdipStartPathFigure": lambda dp, p: gp.path_close(dp, p),
        "GdipFillPath": gp.fill_path,
        "GdipDrawPath": gp.draw_path,
        "GdipCreateFontFamilyFromName": gp.family,
        "GdipGetGenericFontFamilySansSerif": lambda dp, out: gp.generic_family(dp, out, "Microsoft Sans Serif"),
        "GdipGetGenericFontFamilySerif": lambda dp, out: gp.generic_family(dp, out, "Times New Roman"),
        "GdipGetGenericFontFamilyMonospace": lambda dp, out: gp.generic_family(dp, out, "Courier New"),
        "GdipDeleteFontFamily": lambda dp, f: gp.delete(f),
        "GdipCreateFont": gp.font,
        "GdipCreateFontFromDC": gp.font_from_dc,
        "GdipCreateFontFromLogfontW": lambda dp, hdc, lf, out: gp.font_from_logfont(dp, hdc, lf, out, True),
        "GdipCreateFontFromLogfontA": lambda dp, hdc, lf, out: gp.font_from_logfont(dp, hdc, lf, out, False),
        "GdipDeleteFont": lambda dp, f: gp.delete(f),
        "GdipCreateStringFormat": gp.string_format,
        "GdipStringFormatGetGenericDefault": gp.generic_format,
        "GdipStringFormatGetGenericTypographic": gp.generic_format,
        "GdipCloneStringFormat": lambda dp, f, out: gp.new(dict(gp.get(f, dict) or {}), out),
        "GdipDeleteStringFormat": lambda dp, f: gp.delete(f),
        "GdipSetStringFormatAlign": lambda dp, f, a: gp.format_align(dp, f, a, "align"),
        "GdipSetStringFormatLineAlign": lambda dp, f, a: gp.format_align(dp, f, a, "line"),
        "GdipSetStringFormatFlags": lambda dp, f, flags: OK,
        "GdipSetStringFormatTrimming": lambda dp, f, t: OK,
        "GdipSetStringFormatHotkeyPrefix": lambda dp, f, h: OK,
        "GdipDrawString": gp.draw_string,
        "GdipMeasureString": gp.measure_string,
        "GdipCreateBitmapFromScan0": gp.bitmap_scan0,
        "GdipCreateBitmapFromGraphics": gp.bitmap_from_graphics,
        "GdipCreateBitmapFromHBITMAP": gp.bitmap_from_hbitmap,
        "GdipCreateHBITMAPFromBitmap": gp.hbitmap_from_bitmap,
        "GdipCreateBitmapFromFile": gp.load_file,
        "GdipCreateBitmapFromFileICM": gp.load_file,
        "GdipLoadImageFromFile": gp.load_file,
        "GdipLoadImageFromFileICM": gp.load_file,
        "GdipSaveImageToFile": gp.save_file,
        "GdipGetImageEncodersSize": gp.encoders_size,
        "GdipGetImageEncoders": gp.encoders,
        "GdipDisposeImage": gp.dispose,
        "GdipGetImageWidth": lambda dp, i, out: gp.image_size(dp, i, out, 0),
        "GdipGetImageHeight": lambda dp, i, out: gp.image_size(dp, i, out, 1),
        "GdipGetImagePixelFormat": lambda dp, i, out: (dp.write_ulong(out, 0x26200A), OK)[1],
        "GdipBitmapGetPixel": lambda dp, b, x, y, out: gp.pixel(dp, b, x, y, out),
        "GdipBitmapSetPixel": lambda dp, b, x, y, c: gp.pixel(dp, b, x, y, color=c),
        "GdipBitmapLockBits": gp.lock_bits,
        "GdipBitmapUnlockBits": gp.unlock_bits,
        "GdipDrawImage": lambda dp, g, i, x, y: gp.draw_image(dp, g, i, x, y),
        "GdipDrawImageI": lambda dp, g, i, x, y: gp.draw_image(dp, g, i, x, y),
        "GdipDrawImageRect": lambda dp, g, i, x, y, w, h: gp.draw_image(dp, g, i, x, y, w, h),
        "GdipDrawImageRectI": lambda dp, g, i, x, y, w, h: gp.draw_image(dp, g, i, x, y, w, h),
        "GdipDrawImageRectRect": lambda dp, g, i, x, y, w, h, sx, sy, sw, sh, unit, attrs, cb, data: gp.draw_image(dp, g, i, x, y, w, h, (sx, sy, sw, sh)),
        "GdipDrawImageRectRectI": lambda dp, g, i, x, y, w, h, sx, sy, sw, sh, unit, attrs, cb, data: gp.draw_image(dp, g, i, x, y, w, h, (sx, sy, sw, sh)),
    }
    for name in ("GdipSetSmoothingMode", "GdipSetTextRenderingHint", "GdipSetInterpolationMode", "GdipSetPixelOffsetMode",
                 "GdipSetCompositingMode", "GdipSetCompositingQuality", "GdipSetPageUnit", "GdipSetPageScale",
                 "GdipSetTextContrast", "GdipSetPenDashStyle", "GdipSetPenStartCap", "GdipSetPenEndCap", "GdipSetPenLineJoin",
                 "GdipSetPenLineCap197819", "GdipSetLineGammaCorrection", "GdipSetLineWrapMode"):
        implemented[name] = lambda dp, *a: OK
    for name, signature in GDIPLUS.items():
        handler = implemented.get(name)
        if handler is None:
            handler = (lambda n: lambda dp, *a: _not_implemented(dp, n))(name)
        hooks.register_signature(m, name, signature, handler)
    return gp


def _not_implemented(dp, name: str) -> int:
    missing = dp._fathom_unimplemented
    if name not in missing:
        dp.error(f"GDI+ {name} is not emulated: NotImplemented")
    missing[name] = missing.get(name, 0) + 1
    return NOT_IMPLEMENTED
