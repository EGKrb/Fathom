# Fathom addition to dumpulator: what the windows of the emulated program show, drawn into images
# (Pillow). Boost Software License 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# Each window has a canvas the size of its client area. ShowWindow and InvalidateRect queue
# WM_PAINT, UpdateWindow sends it: the program paints as natively, through BeginPaint / GetDC and the
# functions of gdi32 drawn here (text with its colors and background mode, solid brushes and pens,
# stock objects, Rectangle, Ellipse, MoveToEx / LineTo, SetPixel, FillRect / FrameRect, DrawText with
# its alignment, GetTextExtentPoint32). The controls (BUTTON, STATIC, EDIT) are drawn with their text,
# message boxes as dialogs with their buttons. Images (PNG) of the windows are saved when they are
# destroyed and at the end of the run (Gui.save_screenshots), in the directory of --screenshots.

import os
import re
from dataclasses import dataclass
from typing import Dict, Optional

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # the images are optional
    Image = ImageDraw = ImageFont = None

STOCK = {0: ("brush", 0xFFFFFF), 1: ("brush", 0xC0C0C0), 2: ("brush", 0x808080), 3: ("brush", 0x404040),
         4: ("brush", 0x000000), 5: ("brush", None), 6: ("pen", 0xFFFFFF), 7: ("pen", 0x000000), 8: ("pen", None),
         18: ("brush", 0xFFFFFF), 19: ("pen", 0x000000)}
SYSTEM_COLORS = {0: 0xC8C8C8, 1: 0x000000, 4: 0xF0F0F0, 5: 0xFFFFFF, 6: 0x646464, 8: 0x000000, 15: 0xF0F0F0,
                 16: 0xA0A0A0, 18: 0x000000}
TITLE_HEIGHT = 30


def _rgb(colorref: Optional[int]):
    if colorref is None:
        return None
    return colorref & 0xFF, (colorref >> 8) & 0xFF, (colorref >> 16) & 0xFF


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


@dataclass
class GdiObject:
    kind: str                      # brush, pen, font
    color: Optional[int] = None    # COLORREF (None: null brush / pen)
    width: int = 1
    size: int = 15


class DC:
    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        self.text_color = 0x000000
        self.background = 0xFFFFFF
        self.transparent = False
        self.brush = GdiObject("brush", 0xFFFFFF)
        self.pen = GdiObject("pen", 0x000000)
        self.font = GdiObject("font", size=15)
        self.position = (0, 0)


class Drawing:
    """The canvases of the windows of one emulated process."""

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

    def _draw(self, hdc: int):
        dc = self.dcs.get(hdc)
        if dc is None:
            return None, None
        image = self.canvas(dc.hwnd)
        return dc, (ImageDraw.Draw(image) if image is not None else None)

    def new_object(self, obj: GdiObject) -> int:
        self.next_object += 4
        self.objects[self.next_object] = obj
        return self.next_object

    def object(self, handle: int) -> Optional[GdiObject]:
        if handle in self.objects:
            return self.objects[handle]
        if handle & 0xFFFF in STOCK and handle < 0x100:
            kind, color = STOCK[handle & 0xFFFF]
            return GdiObject(kind, color)
        return None

    def brush_color(self, brush: int) -> Optional[int]:
        if 0 < brush <= 31:  # COLOR_x + 1 (a system color)
            return SYSTEM_COLORS.get(brush - 1, 0xF0F0F0)
        obj = self.object(brush)
        return obj.color if obj is not None else 0xFFFFFF

    # ------------------------------------------------------------------ gdi32
    def select(self, hdc: int, handle: int) -> int:
        dc = self.dcs.get(hdc)
        obj = self.object(handle) if handle < 0x100 else self.objects.get(handle)
        if dc is None or obj is None:
            return 0
        previous = getattr(dc, obj.kind)
        setattr(dc, obj.kind, obj)
        for key, value in self.objects.items():
            if value is previous:
                return key
        return self.new_object(previous)

    def text(self, hdc: int, x: int, y: int, value: str, opaque_rect=None):
        dc, draw = self._draw(hdc)
        if draw is None:
            return
        face = font(dc.font.size)
        if opaque_rect is not None:
            draw.rectangle(opaque_rect, fill=_rgb(dc.background))
        if not dc.transparent:
            box = draw.textbbox((x, y), value, font=face)
            draw.rectangle(box, fill=_rgb(dc.background))
        draw.text((x, y), value, fill=_rgb(dc.text_color), font=face)

    def text_extent(self, hdc: int, value: str):
        dc = self.dcs.get(hdc)
        face = font(dc.font.size if dc else 15)
        if face is None:
            return 8 * len(value), 16
        box = face.getbbox(value or " ")
        return box[2] - box[0] if value else 0, max(16, box[3])

    def draw_text(self, hdc: int, value: str, rect, flags: int):
        dc, draw = self._draw(hdc)
        if draw is None:
            return
        left, top, right, bottom = rect
        width, height = self.text_extent(hdc, value)
        x = left + (right - left - width) // 2 if flags & 0x1 else (right - width if flags & 0x2 else left)
        y = top + (bottom - top - height) // 2 if flags & 0x4 and flags & 0x20 else (bottom - height if flags & 0x8 else top)
        self.text(hdc, x, y, value)

    def rectangle(self, hdc: int, box, ellipse=False):
        dc, draw = self._draw(hdc)
        if draw is None:
            return
        fill, outline = _rgb(dc.brush.color), _rgb(dc.pen.color)
        box = (box[0], box[1], max(box[0], box[2] - 1), max(box[1], box[3] - 1))
        (draw.ellipse if ellipse else draw.rectangle)(box, fill=fill, outline=outline, width=max(1, dc.pen.width))

    def fill(self, hdc: int, box, color: Optional[int], frame=False):
        dc, draw = self._draw(hdc)
        if draw is None or color is None:
            return
        box = (box[0], box[1], max(box[0], box[2] - 1), max(box[1], box[3] - 1))
        if frame:
            draw.rectangle(box, outline=_rgb(color))
        else:
            draw.rectangle(box, fill=_rgb(color))

    def line_to(self, hdc: int, x: int, y: int):
        dc, draw = self._draw(hdc)
        if draw is None:
            return
        if dc.pen.color is not None:
            draw.line([dc.position, (x, y)], fill=_rgb(dc.pen.color), width=max(1, dc.pen.width))
        dc.position = (x, y)

    def pixel(self, hdc: int, x: int, y: int, color: int):
        dc, draw = self._draw(hdc)
        if draw is not None:
            draw.point((x, y), fill=_rgb(color))

    # ------------------------------------------------------------------ images
    def compose(self, window):
        """The image of a top-level window: a title bar, its client area, its controls."""
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
        self._saved_windows = getattr(self, "_saved_windows", set()) | {window.hwnd}
        return self.save(self.compose(window), window.title or window.class_name)
