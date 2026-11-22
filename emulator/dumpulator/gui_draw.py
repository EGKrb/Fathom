# Fathom addition to dumpulator: what the windows of the emulated program show, drawn into images
# (Pillow). Boost Software License 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# Each window has a canvas the size of its client area. ShowWindow and InvalidateRect queue
# WM_PAINT, UpdateWindow sends it: the program paints as natively, through BeginPaint / GetDC and the
# functions of gdi32 drawn here:
# - text with its colors and background mode, DrawText with its alignment, GetTextExtentPoint32;
# - solid brushes and pens, stock objects, Rectangle, Ellipse, RoundRect, Polygon, Polyline,
#   PolyBezier, Arc, Pie, Chord, MoveToEx / LineTo, SetPixel / GetPixel, FillRect / FrameRect;
# - bitmaps: memory DCs (CreateCompatibleDC), CreateCompatibleBitmap, CreateBitmap, CreateDIBitmap,
#   DIB sections (their pixels are in the program's memory, read and written there), BitBlt /
#   StretchBlt with the usual raster operations, PatBlt, StretchDIBits, SetDIBitsToDevice,
#   GetDIBits / SetDIBits, TransparentBlt, AlphaBlend, LoadBitmap / LoadImage (from the resources of
#   a module or a .bmp file), GetObject;
# - regions: rectangles, ellipses, rounded rectangles, polygons, CombineRgn (and, or, xor, diff,
#   copy), OffsetRgn, GetRgnBox, PtInRegion, FillRgn / PaintRgn / FrameRgn / InvertRgn, clipping
#   (SelectClipRgn, ExtSelectClipRgn, IntersectClipRect, ExcludeClipRect, SelectObject of a region),
#   SetWindowRgn (the shape of the window in its image);
# - paths: BeginPath ... EndPath around lines, Béziers, arcs and shapes, then StrokePath, FillPath,
#   StrokeAndFillPath (with SetPolyFillMode), SelectClipPath, PathToRegion;
# - SaveDC / RestoreDC.
# The controls (BUTTON, STATIC, EDIT) are drawn with their text, message boxes as dialogs with their
# buttons. Images (PNG) of the windows are saved when they are destroyed and at the end of the run
# (Gui.save_screenshots), in the directory of --screenshots.

import copy
import io
import math
import os
import re
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFont
except ImportError:  # the images are optional
    Image = ImageChops = ImageDraw = ImageFont = None

STOCK = {0: ("brush", 0xFFFFFF), 1: ("brush", 0xC0C0C0), 2: ("brush", 0x808080), 3: ("brush", 0x404040),
         4: ("brush", 0x000000), 5: ("brush", None), 6: ("pen", 0xFFFFFF), 7: ("pen", 0x000000), 8: ("pen", None),
         18: ("brush", 0xFFFFFF), 19: ("pen", 0x000000)}
SYSTEM_COLORS = {0: 0xC8C8C8, 1: 0x000000, 4: 0xF0F0F0, 5: 0xFFFFFF, 6: 0x646464, 8: 0x000000, 15: 0xF0F0F0,
                 16: 0xA0A0A0, 18: 0x000000}
TITLE_HEIGHT = 30
STOCK_BASE = 0x01900000  # the handles of the stock objects (GetStockObject)
NULLREGION, SIMPLEREGION, COMPLEXREGION = 1, 2, 3


def _rgb(colorref: Optional[int]):
    if colorref is None:
        return None
    return colorref & 0xFF, (colorref >> 8) & 0xFF, (colorref >> 16) & 0xFF


def _colorref(rgb) -> int:
    return rgb[0] | (rgb[1] << 8) | (rgb[2] << 16)


_fonts: Dict[int, object] = {}


def font(size: int = 15):
    if ImageFont is None:
        return None
    size = max(8, min(96, abs(size) or 15))
    if size not in _fonts:
        try:
            _fonts[size] = ImageFont.truetype(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts", "segoeui.ttf"), size)
        except Exception:
            _fonts[size] = ImageFont.load_default()
    return _fonts[size]


def _signed(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


# ---------------------------------------------------------------------------------- regions

class Region:
    """A set of pixels: a mask ("L", 0 / 255) placed at (x, y)."""

    def __init__(self, x: int = 0, y: int = 0, mask=None):
        self.x, self.y = x, y
        self.mask = mask if mask is not None else Image.new("L", (0, 0))

    @staticmethod
    def shape(box, kind: str, radius=(0, 0), points=None, alternate=True) -> "Region":
        if points is not None:
            xs, ys = [p[0] for p in points] or [0], [p[1] for p in points] or [0]
            left, top, right, bottom = min(xs), min(ys), max(xs) + 1, max(ys) + 1
        else:
            left, top, right, bottom = box
        width, height = max(0, min(8192, right - left)), max(0, min(8192, bottom - top))
        mask = Image.new("L", (width, height), 0)
        if width and height:
            draw = ImageDraw.Draw(mask)
            if kind == "rect":
                draw.rectangle((0, 0, width - 1, height - 1), fill=255)
            elif kind == "ellipse":
                draw.ellipse((0, 0, width - 1, height - 1), fill=255)
            elif kind == "round":
                draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=max(0, min(radius) // 2), fill=255)
            elif kind == "polygon" and len(points) >= 3:
                draw.polygon([(x - left, y - top) for x, y in points], fill=255)
        return Region(left, top, mask)

    def box(self):
        found = self.mask.getbbox() if self.mask.size[0] and self.mask.size[1] else None
        if not found:
            return 0, 0, 0, 0
        return self.x + found[0], self.y + found[1], self.x + found[2], self.y + found[3]

    def kind(self) -> int:
        box = self.box()
        if box[2] <= box[0] or box[3] <= box[1]:
            return NULLREGION
        width, height = box[2] - box[0], box[3] - box[1]
        inside = self.mask.crop((box[0] - self.x, box[1] - self.y, box[2] - self.x, box[3] - self.y))
        return SIMPLEREGION if inside.getextrema() == (255, 255) and width * height else COMPLEXREGION

    def placed(self, x: int, y: int, width: int, height: int):
        """The mask on an area of (width, height) at (x, y)."""
        out = Image.new("L", (max(1, width), max(1, height)), 0)
        out.paste(self.mask, (self.x - x, self.y - y))
        return out

    def contains(self, x: int, y: int) -> bool:
        mx, my = x - self.x, y - self.y
        return 0 <= mx < self.mask.size[0] and 0 <= my < self.mask.size[1] and self.mask.getpixel((mx, my)) > 0

    def offset(self, dx: int, dy: int):
        self.x += dx
        self.y += dy

    @staticmethod
    def combine(a: "Region", b: Optional["Region"], mode: int) -> "Region":
        if mode == 5 or b is None:  # RGN_COPY
            return Region(a.x, a.y, a.mask.copy())
        boxes = [r.box() for r in (a, b) if r.kind() != NULLREGION]
        if not boxes:
            return Region()
        left, top = min(x[0] for x in boxes), min(x[1] for x in boxes)
        right, bottom = max(x[2] for x in boxes), max(x[3] for x in boxes)
        width, height = right - left, bottom - top
        ma, mb = a.placed(left, top, width, height), b.placed(left, top, width, height)
        result = {1: ImageChops.multiply, 2: ImageChops.lighter, 3: ImageChops.difference,
                  4: ImageChops.subtract}.get(mode, ImageChops.lighter)(ma, mb)
        return Region(left, top, result)


# ---------------------------------------------------------------------------------- objects

@dataclass
class GdiObject:
    kind: str                       # brush, pen, font, bitmap, region
    color: Optional[int] = None     # COLORREF (None: null brush / pen)
    width: int = 1
    size: int = 15
    image: object = None            # bitmap: its pixels (RGB)
    dib: Optional[tuple] = None     # DIB section: (address, width, height, bpp, stride, top_down)
    region: Optional[Region] = None

    def surface(self, dp):
        """The pixels of a bitmap (a DIB section: read from the program's memory)."""
        if self.dib is not None:
            address, width, height, bpp, stride, top_down = self.dib
            data = bytes(dp.read(address, stride * height))
            self.image = _decode_pixels(data, width, height, bpp, stride, top_down, None)
        return self.image

    def written(self, dp):
        """After a drawing on a DIB section: its pixels back in the program's memory."""
        if self.dib is not None and self.image is not None:
            address, width, height, bpp, stride, top_down = self.dib
            dp.write(address, _encode_pixels(self.image, bpp, stride, top_down))


def _decode_pixels(data: bytes, width: int, height: int, bpp: int, stride: int, top_down: bool, palette):
    orientation = 1 if top_down else -1
    size = (max(1, width), max(1, height))
    try:
        if bpp == 32:
            return Image.frombuffer("RGB", size, data, "raw", "BGRX", stride, orientation)
        if bpp == 24:
            return Image.frombuffer("RGB", size, data, "raw", "BGR", stride, orientation)
        if bpp == 16:
            return Image.frombuffer("RGB", size, data, "raw", "BGR;15", stride, orientation)
        if bpp in (1, 4, 8):
            mode = {1: "P;1", 4: "P;4", 8: "P"}[bpp]
            image = Image.frombuffer("P", size, data, "raw", mode, stride, orientation)
            colors = palette or ([(0, 0, 0), (255, 255, 255)] if bpp == 1 else [(i, i, i) for i in range(256)])
            flat = []
            for color in colors[:256]:
                flat += list(color)
            image.putpalette(flat + [0] * (768 - len(flat)))
            return image.convert("RGB")
    except Exception:
        pass
    return Image.new("RGB", size, (0, 0, 0))


def _encode_pixels(image, bpp: int, stride: int, top_down: bool) -> bytes:
    orientation = 1 if top_down else -1
    if bpp == 32:
        return image.tobytes("raw", "BGRX", stride, orientation)
    if bpp == 24:
        return image.tobytes("raw", "BGR", stride, orientation)
    if bpp == 16:
        return image.tobytes("raw", "BGR;15", stride, orientation)
    return b""


def read_dib(dp, info: int, bits: int):
    """A packed BITMAPINFO at info, pixels at bits (or right after the header and colors when bits
    is 0): (image, width, height, bpp, top_down)."""
    size, width, height, planes, bpp, compression = struct.unpack("<IiiHHI", bytes(dp.read(info, 20)))
    used = dp.read_ulong(info + 32)
    top_down = height < 0
    height = abs(height)
    colors = used or (1 << bpp if bpp <= 8 else 0)
    table = 12 if compression == 3 and bpp in (16, 32) else 0
    palette = None
    if bpp <= 8 and colors:
        raw = bytes(dp.read(info + size, 4 * colors))
        palette = [(raw[i + 2], raw[i + 1], raw[i]) for i in range(0, len(raw), 4)]
    stride = ((width * bpp + 31) // 32) * 4
    if not bits:
        bits = info + size + 4 * (colors if bpp <= 8 else 0) + table
    data = bytes(dp.read(bits, stride * height))
    return _decode_pixels(data, width, height, bpp, stride, top_down, palette), width, height, bpp, top_down


def _premultiplied_over(below, source, constant: int):
    """AlphaBlend with AC_SRC_ALPHA: destination = source * k + destination * (1 - alpha * k), the
    source premultiplied by its alpha, k the constant alpha."""
    k = constant / 255
    try:
        import numpy
        dst = numpy.asarray(below, dtype=numpy.float32)
        src = numpy.asarray(source, dtype=numpy.float32)
        a = src[..., 3:4] * k / 255
        out = src[..., :3] * k + dst * (1 - a)
        return Image.fromarray(numpy.clip(out + 0.5, 0, 255).astype(numpy.uint8), "RGB")
    except ImportError:
        r, g, b, a = source.split()
        weight = a.point(lambda v: 255 - round(v * k))
        kept = ImageChops.multiply(below, Image.merge("RGB", (weight, weight, weight)))
        added = Image.merge("RGB", (r, g, b)).point(lambda v: round(v * k))
        return ImageChops.add(kept, added)


def _bitwise(destination, source, operation):
    """A raster operation byte by byte (big integers: fast enough for a window)."""
    a = int.from_bytes(destination.tobytes(), "little")
    b = int.from_bytes(source.tobytes(), "little")
    length = len(destination.tobytes())
    mask = (1 << (8 * length)) - 1
    value = operation(a, b, mask) & mask
    return Image.frombytes("RGB", destination.size, value.to_bytes(length, "little"))


# ---------------------------------------------------------------------------------- DCs

class DC:
    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        self.text_color = 0x000000
        self.background = 0xFFFFFF
        self.transparent = False
        self.brush = GdiObject("brush", 0xFFFFFF)
        self.pen = GdiObject("pen", 0x000000)
        self.font = GdiObject("font", size=15)
        self.bitmap: Optional[GdiObject] = None
        self.clip: Optional[Region] = None
        self.position = (0, 0)
        self.path: Optional[List[list]] = None   # figures: [points, closed]
        self.path_open = False
        self.fill_mode = 1                        # ALTERNATE
        self.saved: List[dict] = []


def _flatten_bezier(start, points) -> list:
    out = []
    current = start
    for i in range(0, len(points) - 2, 3):
        p1, p2, p3 = points[i], points[i + 1], points[i + 2]
        for step in range(1, 17):
            t = step / 16
            u = 1 - t
            out.append((round(u ** 3 * current[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0]),
                        round(u ** 3 * current[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1])))
        current = p3
    return out


def _arc_points(box, start, end) -> list:
    """The points of an elliptic arc, counterclockwise on the screen from the radial through start to
    the one through end (GDI's default direction)."""
    left, top, right, bottom = box
    cx, cy, rx, ry = (left + right) / 2, (top + bottom) / 2, max(1, (right - left) / 2), max(1, (bottom - top) / 2)
    a0 = math.atan2((start[1] - cy) / ry, (start[0] - cx) / rx)
    a1 = math.atan2((end[1] - cy) / ry, (end[0] - cx) / rx)
    sweep = (a0 - a1) % (2 * math.pi) or 2 * math.pi  # counterclockwise on screen: the angle decreases
    steps = max(8, int(sweep * 16))
    return [(round(cx + rx * math.cos(a0 - sweep * i / steps)), round(cy + ry * math.sin(a0 - sweep * i / steps)))
            for i in range(steps + 1)]


def _ellipse_points(box) -> list:
    left, top, right, bottom = box
    cx, cy, rx, ry = (left + right) / 2, (top + bottom) / 2, (right - left) / 2, (bottom - top) / 2
    return [(round(cx + rx * math.cos(2 * math.pi * i / 64)), round(cy + ry * math.sin(2 * math.pi * i / 64))) for i in range(64)]


class Drawing:
    """The canvases, DCs and GDI objects of one emulated process."""

    def __init__(self, gui):
        self.gui = gui
        self.canvases: Dict[int, object] = {}
        self.dcs: Dict[int, DC] = {}
        self.objects: Dict[int, GdiObject] = {}
        self.next_object = 0x5A0000
        self.saved = []
        self.directory: Optional[str] = None
        self.counter = 0

    @property
    def enabled(self) -> bool:
        return Image is not None

    @property
    def dp(self):
        return self.gui.dp

    # ------------------------------------------------------------------ canvases, DCs, objects
    def canvas(self, hwnd: int):
        if not self.enabled:
            return None
        window = self.gui.windows.get(hwnd)
        if window is None:
            return None
        if hwnd not in self.canvases:
            left, top, right, bottom = window.rect
            width, height = max(1, min(4096, right - left)), max(1, min(4096, bottom - top))
            self.canvases[hwnd] = Image.new("RGB", (width, height), (255, 255, 255) if window.class_name.lower() != "button" else (240, 240, 240))
        return self.canvases[hwnd]

    def new_dc(self, hwnd: int) -> int:
        hdc = self.gui._handle()
        self.dcs[hdc] = DC(hwnd)
        return hdc

    def memory_dc(self, dp, hdc) -> int:
        """CreateCompatibleDC: a DC without a window, a 1x1 bitmap selected."""
        handle = self.new_dc(0)
        self.dcs[handle].bitmap = GdiObject("bitmap", image=Image.new("RGB", (1, 1), (0, 0, 0))) if self.enabled else None
        return handle

    def new_object(self, obj: GdiObject) -> int:
        self.next_object += 4
        self.objects[self.next_object] = obj
        return self.next_object

    def object(self, handle: int) -> Optional[GdiObject]:
        if handle in self.objects:
            return self.objects[handle]
        stock = handle - STOCK_BASE if STOCK_BASE <= handle < STOCK_BASE + 0x20 else (handle if handle < 0x20 else None)
        if stock in STOCK:
            kind, color = STOCK[stock]
            return GdiObject(kind, color)
        return None

    def brush_color(self, brush: int) -> Optional[int]:
        if 0 < brush <= 31:  # COLOR_x + 1 (a system color)
            return SYSTEM_COLORS.get(brush - 1, 0xF0F0F0)
        obj = self.object(brush)
        return obj.color if obj is not None else 0xFFFFFF

    def select(self, hdc: int, handle: int) -> int:
        dc = self.dcs.get(hdc)
        obj = self.object(handle)
        if dc is None or obj is None:
            return 0
        if obj.kind == "region":  # SelectObject of a region: the clipping region (a copy)
            dc.clip = Region.combine(obj.region, None, 5) if obj.region is not None else None
            return SIMPLEREGION if dc.clip is not None and dc.clip.kind() != NULLREGION else NULLREGION
        if obj.kind == "bitmap":
            previous = dc.bitmap
            dc.bitmap = obj
        else:
            previous = getattr(dc, obj.kind)
            setattr(dc, obj.kind, obj)
        for key, value in self.objects.items():
            if value is previous:
                return key
        return self.new_object(previous) if previous is not None else 0

    def _surface(self, dc: DC):
        if dc.bitmap is not None:
            return dc.bitmap.surface(self.dp)
        if dc.hwnd:
            return self.canvas(dc.hwnd)
        return None

    def _paint(self, hdc: int, function):
        """function(draw, dc, image) on the surface of the DC, through its clipping region."""
        dc = self.dcs.get(hdc)
        if dc is None or not self.enabled:
            return
        image = self._surface(dc)
        if image is None:
            return
        if dc.clip is None:
            function(ImageDraw.Draw(image), dc, image)
        else:
            layer = image.copy()
            function(ImageDraw.Draw(layer), dc, layer)
            image.paste(layer, (0, 0), dc.clip.placed(0, 0, *image.size))
        if dc.bitmap is not None:
            dc.bitmap.written(self.dp)

    def save_dc(self, hdc: int) -> int:
        dc = self.dcs.get(hdc)
        if dc is None:
            return 0
        state = {k: copy.copy(v) for k, v in dc.__dict__.items() if k != "saved"}
        dc.saved.append(state)
        return len(dc.saved)

    def restore_dc(self, hdc: int, level: int) -> int:
        dc = self.dcs.get(hdc)
        level = _signed(level)
        if dc is None or not dc.saved:
            return 0
        index = len(dc.saved) + level if level < 0 else level - 1
        if not 0 <= index < len(dc.saved):
            return 0
        state = dc.saved[index]
        del dc.saved[index:]
        dc.__dict__.update(state)
        return 1

    # ------------------------------------------------------------------ text
    def text(self, hdc: int, x: int, y: int, value: str, opaque_rect=None):
        def draw_text(draw, dc, image):
            face = font(dc.font.size)
            if opaque_rect is not None:
                draw.rectangle(opaque_rect, fill=_rgb(dc.background))
            if not dc.transparent:
                draw.rectangle(draw.textbbox((x, y), value, font=face), fill=_rgb(dc.background))
            draw.text((x, y), value, fill=_rgb(dc.text_color), font=face)
        self._paint(hdc, draw_text)

    def text_extent(self, hdc: int, value: str):
        dc = self.dcs.get(hdc)
        face = font(dc.font.size if dc else 15)
        if face is None:
            return 8 * len(value), 16
        box = face.getbbox(value or " ")
        return box[2] - box[0] if value else 0, max(16, box[3])

    def draw_text(self, hdc: int, value: str, rect, flags: int):
        left, top, right, bottom = rect
        width, height = self.text_extent(hdc, value)
        x = left + (right - left - width) // 2 if flags & 0x1 else (right - width if flags & 0x2 else left)
        y = top + (bottom - top - height) // 2 if flags & 0x4 and flags & 0x20 else (bottom - height if flags & 0x8 else top)
        self.text(hdc, x, y, value)

    # ------------------------------------------------------------------ shapes (and paths)
    def _recording(self, hdc: int) -> Optional[DC]:
        dc = self.dcs.get(hdc)
        return dc if dc is not None and dc.path_open else None

    def _figure(self, dc: DC, points, closed: bool):
        dc.path.append([list(points), closed])

    def shape(self, hdc: int, kind: str, box, extra=None):
        """Rectangle, Ellipse, RoundRect, Polygon, Polyline, Pie, Chord, Arc: in the path when one is
        being recorded, else drawn with the pen (outline) and the brush (inside)."""
        dc = self.dcs.get(hdc)
        if dc is None:
            return
        left, top, right, bottom = box if box is not None else (0, 0, 0, 0)
        if kind == "rect":
            points, closed = [(left, top), (right - 1, top), (right - 1, bottom - 1), (left, bottom - 1)], True
        elif kind == "ellipse":
            points, closed = _ellipse_points((left, top, right - 1, bottom - 1)), True
        elif kind == "round":
            points, closed = None, True
        elif kind in ("polygon", "polyline"):
            points, closed = list(extra), kind == "polygon"
        else:  # arc, pie, chord: extra = (start point, end point)
            arc = _arc_points((left, top, right - 1, bottom - 1), extra[0], extra[1])
            if kind == "pie":
                points, closed = [((left + right) // 2, (top + bottom) // 2)] + arc, True
            else:
                points, closed = arc, kind == "chord"
        if dc.path_open:
            if points is None:  # a rounded rectangle in a path: its corners as an ellipse would be
                points = [(left, top), (right - 1, top), (right - 1, bottom - 1), (left, bottom - 1)]
            self._figure(dc, points, closed)
            return

        def draw(d, dc, image):
            fill = _rgb(dc.brush.color) if closed else None
            outline = _rgb(dc.pen.color)
            width = max(1, dc.pen.width)
            if kind == "rect":
                d.rectangle((left, top, max(left, right - 1), max(top, bottom - 1)), fill=fill, outline=outline, width=width)
            elif kind == "ellipse":
                d.ellipse((left, top, max(left, right - 1), max(top, bottom - 1)), fill=fill, outline=outline, width=width)
            elif kind == "round":
                radius = max(0, min(extra) // 2)
                d.rounded_rectangle((left, top, max(left, right - 1), max(top, bottom - 1)), radius=radius, fill=fill,
                                    outline=outline, width=width)
            elif len(points) >= 2:
                if closed and fill is not None and len(points) >= 3:
                    d.polygon(points, fill=fill)
                if outline is not None:
                    d.line(points + ([points[0]] if closed else []), fill=outline, width=width)
        self._paint(hdc, draw)

    def rectangle(self, hdc: int, box, ellipse=False):
        self.shape(hdc, "ellipse" if ellipse else "rect", box)

    def fill(self, hdc: int, box, color: Optional[int], frame=False):
        if color is None:
            return

        def draw(d, dc, image):
            area = (box[0], box[1], max(box[0], box[2] - 1), max(box[1], box[3] - 1))
            if frame:
                d.rectangle(area, outline=_rgb(color))
            else:
                d.rectangle(area, fill=_rgb(color))
        self._paint(hdc, draw)

    def move_to(self, hdc: int, x: int, y: int):
        dc = self.dcs.get(hdc)
        if dc is None:
            return
        if dc.path_open:
            self._figure(dc, [(x, y)], False)
        dc.position = (x, y)

    def line_to(self, hdc: int, x: int, y: int):
        dc = self.dcs.get(hdc)
        if dc is None:
            return
        if dc.path_open:
            if not dc.path or dc.path[-1][1]:
                self._figure(dc, [dc.position], False)
            dc.path[-1][0].append((x, y))
        elif dc.pen.color is not None:
            start = dc.position
            self._paint(hdc, lambda d, dc, image: d.line([start, (x, y)], fill=_rgb(dc.pen.color), width=max(1, dc.pen.width)))
        dc.position = (x, y)

    def poly_to(self, hdc: int, points: list, bezier: bool):
        """PolylineTo / PolyBezierTo: from the current position."""
        dc = self.dcs.get(hdc)
        if dc is None or not points:
            return
        line = _flatten_bezier(dc.position, points) if bezier else points
        if dc.path_open:
            if not dc.path or dc.path[-1][1]:
                self._figure(dc, [dc.position], False)
            dc.path[-1][0].extend(line)
        elif dc.pen.color is not None:
            start = dc.position
            self._paint(hdc, lambda d, dc, image: d.line([start] + line, fill=_rgb(dc.pen.color), width=max(1, dc.pen.width)))
        dc.position = line[-1] if not bezier else points[-1]

    def bezier(self, hdc: int, points: list):
        if len(points) >= 4:
            line = [points[0]] + _flatten_bezier(points[0], points[1:])
            self.shape(hdc, "polyline", None, line)

    def pixel(self, hdc: int, x: int, y: int, color: int):
        self._paint(hdc, lambda d, dc, image: d.point((x, y), fill=_rgb(color)))

    def get_pixel(self, hdc: int, x: int, y: int) -> int:
        dc = self.dcs.get(hdc)
        image = self._surface(dc) if dc is not None and self.enabled else None
        if image is None or not (0 <= x < image.size[0] and 0 <= y < image.size[1]):
            return 0xFFFFFFFF  # CLR_INVALID
        return _colorref(image.getpixel((x, y)))

    # ------------------------------------------------------------------ paths
    def begin_path(self, hdc: int) -> int:
        dc = self.dcs.get(hdc)
        if dc is None:
            return 0
        dc.path, dc.path_open = [], True
        return 1

    def end_path(self, hdc: int) -> int:
        dc = self.dcs.get(hdc)
        if dc is None or not dc.path_open:
            return 0
        dc.path_open = False
        return 1

    def abort_path(self, hdc: int) -> int:
        dc = self.dcs.get(hdc)
        if dc is not None:
            dc.path, dc.path_open = None, False
        return 1

    def close_figure(self, hdc: int) -> int:
        dc = self.dcs.get(hdc)
        if dc is None or not dc.path_open or not dc.path:
            return 0
        dc.path[-1][1] = True
        return 1

    def _path_region(self, dc: DC) -> Optional[Region]:
        figures = [f for f in (dc.path or []) if len(f[0]) >= 3]
        if not figures:
            return Region() if self.enabled else None
        region = None
        for points, _ in figures:
            part = Region.shape(None, "polygon", points=points)
            region = part if region is None else Region.combine(region, part, 3 if dc.fill_mode == 1 else 2)
        return region

    def fill_path(self, hdc: int, stroke: bool, fill: bool) -> int:
        dc = self.dcs.get(hdc)
        if dc is None or dc.path is None or dc.path_open or not self.enabled:
            return 0
        figures = dc.path
        region = self._path_region(dc) if fill else None

        def draw(d, dc, image):
            if region is not None and dc.brush.color is not None:
                image.paste(_rgb(dc.brush.color), (0, 0) + image.size, region.placed(0, 0, *image.size))
            if stroke and dc.pen.color is not None:
                for points, closed in figures:
                    if len(points) >= 2:
                        d.line(points + ([points[0]] if closed else []), fill=_rgb(dc.pen.color), width=max(1, dc.pen.width))
        self._paint(hdc, draw)
        dc.path = None
        return 1

    def select_clip_path(self, hdc: int, mode: int) -> int:
        dc = self.dcs.get(hdc)
        if dc is None or dc.path is None or dc.path_open or not self.enabled:
            return 0
        self.clip(hdc, self._path_region(dc), mode)
        dc.path = None
        return 1

    def path_to_region(self, hdc: int) -> int:
        dc = self.dcs.get(hdc)
        if dc is None or dc.path is None or dc.path_open or not self.enabled:
            return 0
        region = self._path_region(dc)
        dc.path = None
        return self.new_object(GdiObject("region", region=region))

    # ------------------------------------------------------------------ regions
    def new_region(self, kind: str, box=None, radius=(0, 0), points=None, alternate=True) -> int:
        if not self.enabled:
            return self.new_object(GdiObject("region"))
        return self.new_object(GdiObject("region", region=Region.shape(box, kind, radius, points, alternate)))

    def region(self, handle: int) -> Optional[Region]:
        obj = self.objects.get(handle)
        return obj.region if obj is not None and obj.kind == "region" else None

    def combine(self, destination: int, first: int, second: int, mode: int) -> int:
        target = self.objects.get(destination)
        a, b = self.region(first), self.region(second)
        if target is None or a is None or (b is None and mode != 5):
            return 0  # ERROR
        target.kind = "region"
        target.region = Region.combine(a, b, mode)
        return target.region.kind()

    def clip(self, hdc: int, region: Optional[Region], mode: int = 5) -> int:
        """The clipping region of a DC combined with region (RGN_AND 1, OR 2, XOR 3, DIFF 4, COPY 5)."""
        dc = self.dcs.get(hdc)
        if dc is None:
            return 0
        if region is None:
            dc.clip = None
            return SIMPLEREGION
        if dc.clip is None or mode == 5:
            dc.clip = Region.combine(region, None, 5)
        else:
            dc.clip = Region.combine(dc.clip, region, mode)
        return dc.clip.kind()

    def clip_rect(self, hdc: int, box, exclude: bool) -> int:
        dc = self.dcs.get(hdc)
        if dc is None or not self.enabled:
            return 0
        rect = Region.shape(box, "rect")
        if exclude:
            if dc.clip is None:
                image = self._surface(dc)
                width, height = image.size if image is not None else (4096, 4096)
                dc.clip = Region.shape((0, 0, width, height), "rect")
            dc.clip = Region.combine(dc.clip, rect, 4)
        else:
            dc.clip = rect if dc.clip is None else Region.combine(dc.clip, rect, 1)
        return dc.clip.kind()

    def paint_region(self, hdc: int, handle: int, color: Optional[int], invert=False, frame=None) -> int:
        region = self.region(handle)
        if region is None or not self.enabled:
            return 0

        def draw(d, dc, image):
            mask = region.placed(0, 0, *image.size)
            if frame is not None:  # FrameRgn: the border of the region (width, height)
                from PIL import ImageFilter
                inner = mask.filter(ImageFilter.MinFilter(max(3, 2 * max(frame) + 1)))
                mask = ImageChops.subtract(mask, inner)
            if invert:
                inverted = ImageChops.invert(image)
                image.paste(inverted, (0, 0), mask)
            elif color is not None:
                image.paste(_rgb(color), (0, 0) + image.size, mask)
        self._paint(hdc, draw)
        return 1

    # ------------------------------------------------------------------ bitmaps
    def new_bitmap(self, width: int, height: int, image=None) -> int:
        if not self.enabled:
            return self.new_object(GdiObject("bitmap"))
        width, height = max(1, min(8192, abs(width))), max(1, min(8192, abs(height)))
        return self.new_object(GdiObject("bitmap", image=image if image is not None else Image.new("RGB", (width, height), (0, 0, 0))))

    def dib_section(self, dp, info: int, bits_out: int) -> int:
        size, width, height, planes, bpp = struct.unpack("<IiiHH", bytes(dp.read(info, 16)))
        stride = ((width * bpp + 31) // 32) * 4
        length = stride * abs(height)
        address = dp.allocate((length + 0xFFF) & ~0xFFF or 0x1000, page_align=True)
        dp.write(address, bytes(length))
        if bits_out:
            dp.write_ptr(bits_out, address)
        obj = GdiObject("bitmap", dib=(address, width, abs(height), bpp if bpp in (16, 24, 32) else 32, stride, height < 0))
        obj.image = Image.new("RGB", (max(1, width), max(1, abs(height))), (0, 0, 0)) if self.enabled else None
        return self.new_object(obj)

    def bitmap_from_bits(self, dp, width: int, height: int, bpp: int, bits: int) -> int:
        handle = self.new_bitmap(width, height)
        obj = self.objects[handle]
        if bits and self.enabled and bpp in (1, 24, 32):
            stride = ((abs(width) * bpp + 15) // 16) * 2  # CreateBitmap: rows aligned on 16 bits
            data = bytes(dp.read(bits, stride * abs(height)))
            obj.image = _decode_pixels(data, abs(width), abs(height), bpp, stride, True, None)
        return handle

    def set_bits(self, dp, handle: int, info: int, bits: int) -> int:
        obj = self.objects.get(handle)
        if obj is None or obj.kind != "bitmap" or not self.enabled:
            return 0
        image, width, height, bpp, top_down = read_dib(dp, info, bits)
        obj.image = image
        obj.written(dp)
        return height

    def get_bits(self, dp, handle: int, start: int, lines: int, bits: int, info: int) -> int:
        """GetDIBits: the size in the header when bits is NULL, else the pixels (24 or 32 bpp)."""
        obj = self.objects.get(handle)
        if obj is None or obj.kind != "bitmap":
            return 0
        image = obj.surface(dp)
        width, height = image.size if image is not None else (1, 1)
        size, _, wanted_height, planes, bpp = struct.unpack("<IiiHH", bytes(dp.read(info, 16)))
        if not bits or bpp == 0:
            dp.write(info + 4, struct.pack("<iiHHI", width, height, 1, 32, 0))
            dp.write_ulong(info + 20, width * height * 4)
            return height
        if bpp not in (24, 32) or image is None:
            return 0
        stride = ((width * bpp + 31) // 32) * 4
        data = _encode_pixels(image, bpp, stride, wanted_height < 0)
        rows = min(lines, height - start)
        offset = start * stride
        dp.write(bits, data[offset:offset + rows * stride])
        return rows

    def load_bitmap(self, dp, instance: int, name: int, from_file: bool, wide: bool) -> int:
        """A bitmap of the resources of a module (RT_BITMAP), or of a .bmp file."""
        if not self.enabled:
            return 0
        from .apihooks import read_string
        try:
            if from_file:
                from .filesystem import filesystem
                path = read_string(dp, name, wide) or ""
                return self.new_bitmap(1, 1, Image.open(io.BytesIO(filesystem(dp).read(path))).convert("RGB"))
            module = dp.modules.find(instance) if instance else dp.modules.find(dp.modules.main)
            if module is None:
                return 0
            import pefile
            pe = pefile.PE(data=open(module.path, "rb").read())
            key = name if name < 0x10000 else (read_string(dp, name, wide) or "").upper()
            for kind in getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None).entries:
                if kind.id != 2:  # RT_BITMAP
                    continue
                for entry in kind.directory.entries:
                    matches = entry.id == key if isinstance(key, int) else (entry.name is not None and str(entry.name).upper() == key)
                    if not matches:
                        continue
                    language = entry.directory.entries[0]
                    data = pe.get_data(language.data.struct.OffsetToData, language.data.struct.Size)
                    header = struct.unpack_from("<I", data)[0]
                    bpp = struct.unpack_from("<H", data, 14)[0]
                    used = struct.unpack_from("<I", data, 32)[0]
                    colors = used or (1 << bpp if bpp <= 8 else 0)
                    offset = 14 + header + 4 * colors
                    bmp = b"BM" + struct.pack("<IHHI", 14 + len(data), 0, 0, offset) + data
                    return self.new_bitmap(1, 1, Image.open(io.BytesIO(bmp)).convert("RGB"))
        except Exception:
            return 0
        return 0

    def object_information(self, dp, handle: int, size: int, out: int) -> int:
        """GetObject: BITMAP of a bitmap, LOGBRUSH of a brush, LOGPEN of a pen."""
        obj = self.object(handle)
        if obj is None:
            return 0
        ptr = dp.ptr_size()
        if obj.kind == "bitmap":
            image = obj.surface(dp) if self.enabled else None
            width, height = image.size if image is not None else (1, 1)
            bpp = obj.dib[3] if obj.dib else 32
            bits = obj.dib[0] if obj.dib else 0
            data = struct.pack("<iiiiHH", 0, width, height, ((width * bpp + 15) // 16) * 2, 1, bpp)
            data += (bytes(4) + struct.pack("<Q", bits)) if ptr == 8 else struct.pack("<I", bits)
        elif obj.kind == "brush":
            data = struct.pack("<II", 1 if obj.color is None else 0, obj.color or 0) + bytes(ptr)
        elif obj.kind == "pen":
            data = struct.pack("<IiiI", 5 if obj.color is None else 0, obj.width, 0, obj.color or 0)
        else:
            return 0
        if not out:
            return len(data)
        data = data[:size]
        dp.write(out, data)
        return len(data)

    def _source(self, hdc: int):
        dc = self.dcs.get(hdc)
        return self._surface(dc) if dc is not None and self.enabled else None

    def _source_alpha(self, hdc: int):
        """The source of an AlphaBlend with AC_SRC_ALPHA: RGBA (the alpha of a 32-bit DIB section, else
        opaque)."""
        dc = self.dcs.get(hdc)
        if dc is None or not self.enabled:
            return None
        bitmap = dc.bitmap
        if bitmap is not None and bitmap.dib is not None and bitmap.dib[3] == 32:
            address, width, height, bpp, stride, top_down = bitmap.dib
            data = bytes(self.dp.read(address, stride * height))
            return Image.frombuffer("RGBA", (max(1, width), max(1, height)), data, "raw", "BGRA", stride, 1 if top_down else -1)
        image = self._surface(dc)
        return image.convert("RGBA") if image is not None else None

    def blit(self, hdc: int, x: int, y: int, width: int, height: int, source_image, sx: int, sy: int, sw: int, sh: int,
             rop: int, transparent: Optional[int] = None, alpha: Optional[int] = None) -> int:
        """BitBlt / StretchBlt / StretchDIBits / TransparentBlt / AlphaBlend onto the DC's surface."""
        if not self.enabled:
            return 1
        rop &= 0xFFFFFFFF

        def draw(d, dc, image):
            area = (x, y, x + abs(width), y + abs(height))
            if rop in (0x00F00021, 0x005A0049, 0x00000042, 0x00FF0062, 0x00550009) and source_image is None:
                if rop == 0x00F00021:   # PATCOPY
                    color = dc.brush.color
                    if color is not None:
                        d.rectangle((x, y, x + abs(width) - 1, y + abs(height) - 1), fill=_rgb(color))
                elif rop == 0x00000042:  # BLACKNESS
                    d.rectangle((x, y, x + abs(width) - 1, y + abs(height) - 1), fill=(0, 0, 0))
                elif rop == 0x00FF0062:  # WHITENESS
                    d.rectangle((x, y, x + abs(width) - 1, y + abs(height) - 1), fill=(255, 255, 255))
                else:                    # DSTINVERT, PATINVERT
                    part = image.crop(area)
                    image.paste(ImageChops.invert(part), area[:2])
                return
            if source_image is None:
                return
            part = source_image.crop((sx, sy, sx + abs(sw), sy + abs(sh)))
            if (abs(sw), abs(sh)) != (abs(width), abs(height)):
                part = part.resize((max(1, abs(width)), max(1, abs(height))))
            if (width < 0) != (sw < 0):
                part = part.transpose(Image.FLIP_LEFT_RIGHT)
            if (height < 0) != (sh < 0):
                part = part.transpose(Image.FLIP_TOP_BOTTOM)
            if transparent is not None:  # TransparentBlt: the pixels of that color are not copied
                key = Image.new("RGB", part.size, _rgb(transparent))
                mask = ImageChops.difference(part, key).convert("L").point(lambda v: 255 if v else 0)
                image.paste(part, area[:2], mask)
                return
            if alpha is not None:  # AlphaBlend: constant alpha, and the alpha of each pixel (premultiplied)
                below = image.crop(area)
                if part.mode == "RGBA":
                    image.paste(_premultiplied_over(below, part, alpha), area[:2])
                else:
                    image.paste(Image.blend(below, part.convert("RGB"), alpha / 255), area[:2])
                return
            if rop == 0x00CC0020:    # SRCCOPY
                image.paste(part, area[:2])
                return
            below = image.crop(area)
            operations = {
                0x00EE0086: lambda a, b, m: a | b,          # SRCPAINT
                0x008800C6: lambda a, b, m: a & b,          # SRCAND
                0x00660046: lambda a, b, m: a ^ b,          # SRCINVERT
                0x00330008: lambda a, b, m: ~b,             # NOTSRCCOPY
                0x00440328: lambda a, b, m: (~a) & b,       # SRCERASE
                0x001100A6: lambda a, b, m: ~(a | b),       # NOTSRCERASE
                0x00BB0226: lambda a, b, m: a | ~b,         # MERGEPAINT
            }
            operation = operations.get(rop)
            image.paste(_bitwise(below, part, operation) if operation else part, area[:2])
        self._paint(hdc, draw)
        return 1

    # ------------------------------------------------------------------ images
    def compose(self, window):
        """The image of a top-level window: a title bar, its client area, its controls, its shape."""
        image = self.canvas(window.hwnd)
        if image is None:
            return None
        width, height = image.size
        result = Image.new("RGB", (width + 2, height + TITLE_HEIGHT + 1), (255, 255, 255))
        draw = ImageDraw.Draw(result)
        draw.rectangle((0, 0, width + 1, TITLE_HEIGHT - 1), fill=(32, 32, 32))
        draw.text((10, 6), window.title or window.class_name, fill=(255, 255, 255), font=font(15))
        draw.text((width - 22, 5), "X", fill=(255, 255, 255), font=font(15))
        result.paste(image, (1, TITLE_HEIGHT))
        draw.rectangle((0, 0, width + 1, height + TITLE_HEIGHT), outline=(96, 96, 96))
        for child in self.gui.windows.values():
            if child.parent == window.hwnd and child.hwnd != window.hwnd:
                self._control(result, child, (1, TITLE_HEIGHT))
        region = getattr(window, "region", None)
        if region is not None:  # SetWindowRgn: outside of the region, the window is transparent
            shaped = result.convert("RGBA")
            shaped.putalpha(region.placed(0, 0, *result.size))
            return shaped
        return result

    def _control(self, image, control, origin):
        draw = ImageDraw.Draw(image)
        left, top, right, bottom = control.rect
        box = (origin[0] + left, origin[1] + top, origin[0] + max(left, right - 1), origin[1] + max(top, bottom - 1))
        kind = control.class_name.lower()
        face = font(14)
        if control.hwnd in self.canvases and kind not in ("button", "static", "edit"):
            image.paste(self.canvases[control.hwnd], box[:2])
            return
        if kind == "button":
            draw.rectangle(box, fill=(225, 225, 225), outline=(112, 112, 112))
            text_box = draw.textbbox((0, 0), control.title, font=face)
            draw.text(((box[0] + box[2] - text_box[2]) // 2, (box[1] + box[3] - text_box[3]) // 2), control.title,
                      fill=(0, 0, 0), font=face)
        elif kind == "edit":
            draw.rectangle(box, fill=(255, 255, 255), outline=(122, 122, 122))
            draw.text((box[0] + 4, box[1] + 3), control.title, fill=(0, 0, 0), font=face)
        else:
            draw.text((box[0], box[1]), control.title, fill=(0, 0, 0), font=face)

    def message_box(self, caption: str, text: str, buttons):
        if not self.enabled:
            return None
        face = font(15)
        lines = (text or "").split("\n")
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        width = max([probe.textbbox((0, 0), line, font=face)[2] for line in lines] + [200]) + 60
        width = max(width, 90 * len(buttons) + 40)
        height = TITLE_HEIGHT + 30 + 22 * len(lines) + 60
        image = Image.new("RGB", (width, height), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, width - 1, TITLE_HEIGHT - 1), fill=(32, 32, 32))
        draw.text((10, 6), caption or "Error", fill=(255, 255, 255), font=face)
        for i, line in enumerate(lines):
            draw.text((30, TITLE_HEIGHT + 20 + 22 * i), line, fill=(0, 0, 0), font=face)
        draw.rectangle((0, height - 50, width - 1, height - 1), fill=(240, 240, 240))
        for i, label in enumerate(reversed(buttons)):
            right = width - 15 - i * 90
            draw.rectangle((right - 80, height - 38, right, height - 12), fill=(225, 225, 225), outline=(0, 120, 215) if i == len(buttons) - 1 else (112, 112, 112))
            text_box = draw.textbbox((0, 0), label, font=face)
            draw.text((right - 40 - text_box[2] // 2, height - 35), label, fill=(0, 0, 0), font=face)
        draw.rectangle((0, 0, width - 1, height - 1), outline=(96, 96, 96))
        return image

    def save(self, image, name: str) -> Optional[str]:
        if image is None or not self.directory:
            return None
        os.makedirs(self.directory, exist_ok=True)
        self.counter += 1
        safe = re.sub(r"[^\w\- ]", "_", name)[:40].strip() or "window"
        path = os.path.join(self.directory, f"{self.gui.dp.process_id}_{self.counter:02d}_{safe}.png")
        image.save(path)
        self.saved.append(path)
        self.gui._record("screenshot", path=path)
        return path

    def save_window(self, window):
        if window.parent or window.dialog or window.hwnd in getattr(self, "_saved_windows", set()):
            return None
        if window.gui is not None and window.gui is not self.gui:
            return None
        self._saved_windows = getattr(self, "_saved_windows", set()) | {window.hwnd}
        return self.save(self.compose(window), window.title or window.class_name)


# ---------------------------------------------------------------------------------- the hooks

def _points(dp, address: int, count: int) -> list:
    data = bytes(dp.read(address, 8 * count)) if count else b""
    return [struct.unpack_from("<ii", data, 8 * i) for i in range(count)]


def _rect(dp, address: int):
    return struct.unpack("<iiii", bytes(dp.read(address, 16)))


def register(hooks, gui):
    """The functions of gdi32 (and some of user32 and msimg32) that draw bitmaps, regions, paths and
    shapes (the others are registered in gui.py)."""
    draw = gui.drawing
    s = _signed
    g = "gdi32.dll"
    # shapes
    hooks.register(g, "RoundRect", 7, lambda dp, h, l, t, r, b, w, hh: (draw.shape(h, "round", (s(l), s(t), s(r), s(b)), (s(w), s(hh))), 1)[1])
    hooks.register(g, "Polygon", 3, lambda dp, h, p, n: (draw.shape(h, "polygon", None, _points(dp, p, n)), 1)[1])
    hooks.register(g, "Polyline", 3, lambda dp, h, p, n: (draw.shape(h, "polyline", None, _points(dp, p, n)), 1)[1])
    hooks.register(g, "PolyBezier", 3, lambda dp, h, p, n: (draw.bezier(h, _points(dp, p, n)), 1)[1])
    hooks.register(g, "PolylineTo", 3, lambda dp, h, p, n: (draw.poly_to(h, _points(dp, p, n), False), 1)[1])
    hooks.register(g, "PolyBezierTo", 3, lambda dp, h, p, n: (draw.poly_to(h, _points(dp, p, n), True), 1)[1])
    for name, kind in (("Arc", "arc"), ("Pie", "pie"), ("Chord", "chord")):
        hooks.register(g, name, 9, lambda dp, h, l, t, r, b, x1, y1, x2, y2, k=kind: (
            draw.shape(h, k, (s(l), s(t), s(r), s(b)), ((s(x1), s(y1)), (s(x2), s(y2)))), 1)[1])
    hooks.register(g, "GdiFlush", 0, lambda dp: 1)
    hooks.register(g, "GetPixel", 3, lambda dp, h, x, y: draw.get_pixel(h, s(x), s(y)))
    hooks.register(g, "SaveDC", 1, lambda dp, h: draw.save_dc(h))
    hooks.register(g, "RestoreDC", 2, lambda dp, h, n: draw.restore_dc(h, n))
    # paths
    hooks.register(g, "BeginPath", 1, lambda dp, h: draw.begin_path(h))
    hooks.register(g, "EndPath", 1, lambda dp, h: draw.end_path(h))
    hooks.register(g, "AbortPath", 1, lambda dp, h: draw.abort_path(h))
    hooks.register(g, "CloseFigure", 1, lambda dp, h: draw.close_figure(h))
    hooks.register(g, "StrokePath", 1, lambda dp, h: draw.fill_path(h, True, False))
    hooks.register(g, "FillPath", 1, lambda dp, h: draw.fill_path(h, False, True))
    hooks.register(g, "StrokeAndFillPath", 1, lambda dp, h: draw.fill_path(h, True, True))
    hooks.register(g, "FlattenPath", 1, lambda dp, h: 1)
    hooks.register(g, "WidenPath", 1, lambda dp, h: 1)
    hooks.register(g, "SelectClipPath", 2, lambda dp, h, m: draw.select_clip_path(h, m))
    hooks.register(g, "PathToRegion", 1, lambda dp, h: draw.path_to_region(h))

    def fill_mode(dp, h, mode):
        dc = draw.dcs.get(h)
        if dc is None:
            return 0
        previous, dc.fill_mode = dc.fill_mode, mode
        return previous
    hooks.register(g, "SetPolyFillMode", 2, fill_mode)
    hooks.register(g, "GetPolyFillMode", 1, lambda dp, h: draw.dcs[h].fill_mode if h in draw.dcs else 0)
    # regions
    hooks.register(g, "CreateRectRgn", 4, lambda dp, l, t, r, b: draw.new_region("rect", (s(l), s(t), s(r), s(b))))
    hooks.register(g, "CreateRectRgnIndirect", 1, lambda dp, r: draw.new_region("rect", _rect(dp, r)))
    hooks.register(g, "CreateEllipticRgn", 4, lambda dp, l, t, r, b: draw.new_region("ellipse", (s(l), s(t), s(r), s(b))))
    hooks.register(g, "CreateEllipticRgnIndirect", 1, lambda dp, r: draw.new_region("ellipse", _rect(dp, r)))
    hooks.register(g, "CreateRoundRectRgn", 6, lambda dp, l, t, r, b, w, hh: draw.new_region("round", (s(l), s(t), s(r), s(b)), (s(w), s(hh))))
    hooks.register(g, "CreatePolygonRgn", 3, lambda dp, p, n, m: draw.new_region("polygon", points=_points(dp, p, n), alternate=m == 1))

    def poly_polygon_region(dp, p, counts, n, mode):
        handle = None
        offset = 0
        for i in range(n):
            count = dp.read_ulong(counts + 4 * i)
            part = draw.new_region("polygon", points=_points(dp, p + 8 * offset, count))
            offset += count
            if handle is None:
                handle = part
            else:
                draw.combine(handle, handle, part, 3 if mode == 1 else 2)
        return handle or draw.new_region("rect", (0, 0, 0, 0))
    hooks.register(g, "CreatePolyPolygonRgn", 4, poly_polygon_region)
    hooks.register(g, "CombineRgn", 4, lambda dp, d, a, b, m: draw.combine(d, a, b, m))

    def set_rect_region(dp, handle, l, t, r, b):
        obj = draw.objects.get(handle)
        if obj is None:
            return 0
        obj.kind, obj.region = "region", Region.shape((s(l), s(t), s(r), s(b)), "rect") if draw.enabled else None
        return 1
    hooks.register(g, "SetRectRgn", 5, set_rect_region)

    def offset_region(dp, handle, x, y):
        region = draw.region(handle)
        if region is None:
            return 0
        region.offset(s(x), s(y))
        return region.kind()
    hooks.register(g, "OffsetRgn", 3, offset_region)

    def region_box(dp, handle, rect):
        region = draw.region(handle)
        if region is None:
            return 0
        dp.write(rect, struct.pack("<iiii", *region.box()))
        return region.kind()
    hooks.register(g, "GetRgnBox", 2, region_box)
    hooks.register(g, "PtInRegion", 3, lambda dp, h, x, y: int(draw.region(h) is not None and draw.region(h).contains(s(x), s(y))))

    def equal_regions(dp, a, b):
        ra, rb = draw.region(a), draw.region(b)
        return int(ra is not None and rb is not None and Region.combine(ra, rb, 3).kind() == NULLREGION)
    hooks.register(g, "EqualRgn", 2, equal_regions)
    hooks.register(g, "FillRgn", 3, lambda dp, h, r, b: draw.paint_region(h, r, draw.brush_color(b)))
    hooks.register(g, "PaintRgn", 2, lambda dp, h, r: draw.paint_region(h, r, draw.dcs[h].brush.color if h in draw.dcs else None))
    hooks.register(g, "FrameRgn", 5, lambda dp, h, r, b, w, hh: draw.paint_region(h, r, draw.brush_color(b), frame=(s(w), s(hh))))
    hooks.register(g, "InvertRgn", 2, lambda dp, h, r: draw.paint_region(h, r, None, invert=True))
    hooks.register(g, "SelectClipRgn", 2, lambda dp, h, r: draw.clip(h, draw.region(r) if r else None))
    hooks.register(g, "ExtSelectClipRgn", 3, lambda dp, h, r, m: draw.clip(h, draw.region(r) if r else None, m))
    hooks.register(g, "IntersectClipRect", 5, lambda dp, h, l, t, r, b: draw.clip_rect(h, (s(l), s(t), s(r), s(b)), False))
    hooks.register(g, "ExcludeClipRect", 5, lambda dp, h, l, t, r, b: draw.clip_rect(h, (s(l), s(t), s(r), s(b)), True))

    def clip_box(dp, h, rect):
        dc = draw.dcs.get(h)
        if dc is None:
            return 0
        if dc.clip is not None:
            dp.write(rect, struct.pack("<iiii", *dc.clip.box()))
            return dc.clip.kind()
        image = draw._surface(dc) if draw.enabled else None
        width, height = image.size if image is not None else (0, 0)
        dp.write(rect, struct.pack("<iiii", 0, 0, width, height))
        return SIMPLEREGION
    hooks.register(g, "GetClipBox", 2, clip_box)

    def window_region(dp, hwnd, handle, redraw):
        window = gui.windows.get(hwnd)
        if window is None:
            return 0
        region = draw.region(handle)
        window.region = Region.combine(region, None, 5) if region is not None else None
        return 1
    hooks.register("user32.dll", "SetWindowRgn", 3, window_region)
    # bitmaps
    hooks.register(g, "CreateCompatibleDC", 1, lambda dp, h: draw.memory_dc(dp, h))
    hooks.register(g, "DeleteDC", 1, lambda dp, h: int(draw.dcs.pop(h, None) is not None))
    hooks.register(g, "CreateCompatibleBitmap", 3, lambda dp, h, w, hh: draw.new_bitmap(s(w), s(hh)))
    hooks.register(g, "CreateBitmap", 5, lambda dp, w, hh, planes, bpp, bits: draw.bitmap_from_bits(dp, s(w), s(hh), bpp, bits))
    hooks.register(g, "CreateDIBSection", 6, lambda dp, h, info, usage, out, section, offset: draw.dib_section(dp, info, out))

    def create_dib_bitmap(dp, h, header, init, bits, info, usage):
        width, height = struct.unpack("<ii", bytes(dp.read(header + 4, 8)))
        handle = draw.new_bitmap(width, height)
        if init & 4 and bits and info:  # CBM_INIT
            draw.set_bits(dp, handle, info, bits)
        return handle
    hooks.register(g, "CreateDIBitmap", 6, create_dib_bitmap)
    hooks.register(g, "SetDIBits", 7, lambda dp, h, bm, start, lines, bits, info, usage: draw.set_bits(dp, bm, info, bits))
    hooks.register(g, "GetDIBits", 7, lambda dp, h, bm, start, lines, bits, info, usage: draw.get_bits(dp, bm, start, lines, bits, info))
    hooks.register(g, "BitBlt", 9, lambda dp, h, x, y, w, hh, src, sx, sy, rop: draw.blit(
        h, s(x), s(y), s(w), s(hh), draw._source(src) if src else None, s(sx), s(sy), s(w), s(hh), rop))
    hooks.register(g, "StretchBlt", 11, lambda dp, h, x, y, w, hh, src, sx, sy, sw, sh, rop: draw.blit(
        h, s(x), s(y), s(w), s(hh), draw._source(src) if src else None, s(sx), s(sy), s(sw), s(sh), rop))
    hooks.register(g, "PatBlt", 6, lambda dp, h, x, y, w, hh, rop: draw.blit(h, s(x), s(y), s(w), s(hh), None, 0, 0, 0, 0, rop))

    def stretch_dib(dp, h, x, y, w, hh, sx, sy, sw, sh, bits, info, usage, rop):
        if not draw.enabled:
            return s(hh)
        image, width, height, bpp, top_down = read_dib(dp, info, bits)
        sy, sh = s(sy), s(sh)
        if not top_down:  # ySrc counts from the bottom of a bottom-up bitmap
            sy = height - sy - abs(sh)
        draw.blit(h, s(x), s(y), s(w), s(hh), image, s(sx), sy, s(sw), sh, rop)
        return abs(sh)
    hooks.register(g, "StretchDIBits", 13, stretch_dib)

    def set_dib_to_device(dp, h, x, y, w, hh, sx, sy, start, lines, bits, info, usage):
        if not draw.enabled:
            return lines
        image, width, height, bpp, top_down = read_dib(dp, info, bits)
        draw.blit(h, s(x), s(y), s(w), s(hh), image, s(sx), max(0, height - s(sy) - s(hh)) if not top_down else s(sy), s(w), s(hh), 0xCC0020)
        return lines
    hooks.register(g, "SetDIBitsToDevice", 12, set_dib_to_device)
    for module in ("msimg32.dll", "gdi32.dll"):
        hooks.register(module, "TransparentBlt", 11, lambda dp, h, x, y, w, hh, src, sx, sy, sw, sh, color: draw.blit(
            h, s(x), s(y), s(w), s(hh), draw._source(src), s(sx), s(sy), s(sw), s(sh), 0xCC0020, transparent=color & 0xFFFFFF))
        # BLENDFUNCTION {BlendOp, BlendFlags, SourceConstantAlpha, AlphaFormat} by value: AC_SRC_ALPHA = 1 in the high byte
        hooks.register(module, "AlphaBlend", 11, lambda dp, h, x, y, w, hh, src, sx, sy, sw, sh, blend: draw.blit(
            h, s(x), s(y), s(w), s(hh), draw._source_alpha(src) if (blend >> 24) & 1 else draw._source(src),
            s(sx), s(sy), s(sw), s(sh), 0xCC0020, alpha=(blend >> 16) & 0xFF))
    for wide, suffix in ((True, "W"), (False, "A")):
        hooks.register(g, "GetObject" + suffix, 3, lambda dp, h, n, out: draw.object_information(dp, h, n, out))
        hooks.register("user32.dll", "LoadBitmap" + suffix, 2, lambda dp, inst, name, w=wide: draw.load_bitmap(dp, inst, name, False, w))
