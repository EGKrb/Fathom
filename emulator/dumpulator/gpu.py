# Fathom addition to dumpulator: what the emulated Direct3D 9 / 10 / 11 / 12 share: the pixel formats
# (DXGI and Direct3D 9, block-compressed BC1-BC5 included), images (render targets, textures, depth
# buffers), the views the shaders read and write, samplers. Boost Software License 1.0, like the rest
# of dumpulator (LICENSE.dumpulator).
#
# An image keeps its pixels as RGBA bytes for the 8-bit formats (what a window shows), as (h, w, 4)
# float32 for the others, and converts from / to the program's bytes at Map, Lock, UpdateSubresource and
# copies to staging resources.

from typing import Dict, List, Optional

from .raster import Framebuffer, numpy

# ------------------------------------------------------------------ formats
# kind: (numpy dtype, channels in memory, class, order)  order: where each memory channel goes (r g b a,
# x: ignored, l: luminance to r g b)
SPECS = {
    "rgba8": ("u1", 4, "unorm", "rgba"), "bgra8": ("u1", 4, "unorm", "bgra"), "bgrx8": ("u1", 4, "unorm", "bgrx"),
    "rgbx8": ("u1", 4, "unorm", "rgbx"), "rgba8sn": ("i1", 4, "snorm", "rgba"), "rgba8u": ("u1", 4, "uint", "rgba"),
    "rgba8i": ("i1", 4, "sint", "rgba"),
    "rgba32f": ("f4", 4, "float", "rgba"), "rgb32f": ("f4", 3, "float", "rgb"), "rg32f": ("f4", 2, "float", "rg"),
    "r32f": ("f4", 1, "float", "r"), "rgba32u": ("u4", 4, "uint", "rgba"), "rgb32u": ("u4", 3, "uint", "rgb"),
    "rg32u": ("u4", 2, "uint", "rg"), "r32u": ("u4", 1, "uint", "r"), "rgba32i": ("i4", 4, "sint", "rgba"),
    "rgb32i": ("i4", 3, "sint", "rgb"), "rg32i": ("i4", 2, "sint", "rg"), "r32i": ("i4", 1, "sint", "r"),
    "rgba16f": ("f2", 4, "float", "rgba"), "rg16f": ("f2", 2, "float", "rg"), "r16f": ("f2", 1, "float", "r"),
    "rgba16un": ("u2", 4, "unorm", "rgba"), "rg16un": ("u2", 2, "unorm", "rg"), "r16un": ("u2", 1, "unorm", "r"),
    "rgba16u": ("u2", 4, "uint", "rgba"), "rg16u": ("u2", 2, "uint", "rg"), "r16u": ("u2", 1, "uint", "r"),
    "rgba16sn": ("i2", 4, "snorm", "rgba"), "rg16sn": ("i2", 2, "snorm", "rg"), "r16sn": ("i2", 1, "snorm", "r"),
    "rgba16i": ("i2", 4, "sint", "rgba"), "rg16i": ("i2", 2, "sint", "rg"), "r16i": ("i2", 1, "sint", "r"),
    "rg8un": ("u1", 2, "unorm", "rg"), "r8un": ("u1", 1, "unorm", "r"), "a8un": ("u1", 1, "unorm", "a"),
    "rg8u": ("u1", 2, "uint", "rg"), "r8u": ("u1", 1, "uint", "r"), "rg8sn": ("i1", 2, "snorm", "rg"),
    "r8sn": ("i1", 1, "snorm", "r"), "rg8i": ("i1", 2, "sint", "rg"), "r8i": ("i1", 1, "sint", "r"),
    "l8": ("u1", 1, "unorm", "l"), "a8l8": ("u1", 2, "unorm", "la"), "l16": ("u2", 1, "unorm", "l"),
    "d32f": ("f4", 1, "depth", "r"), "d16": ("u2", 1, "depth", "r"),
}
# packed and compressed kinds: bytes per pixel (or per 4x4 block)
PACKED = {"rgb10a2un": 4, "bgr10a2un": 4, "rgb10a2u": 4, "r11g11b10f": 4, "rgb9e5": 4, "b5g6r5": 2, "bgr5a1": 2,
          "bgra4": 2, "d24s8": 4, "d32fs8": 8}
BLOCKS = {"bc1": 8, "bc2": 16, "bc3": 16, "bc4": 8, "bc4s": 8, "bc5": 16, "bc5s": 16, "bc6h": 16, "bc6hs": 16, "bc7": 16}

DXGI = {1: "rgba32f", 2: "rgba32f", 3: "rgba32u", 4: "rgba32i", 5: "rgb32f", 6: "rgb32f", 7: "rgb32u", 8: "rgb32i",
        9: "rgba16f", 10: "rgba16f", 11: "rgba16un", 12: "rgba16u", 13: "rgba16sn", 14: "rgba16i", 15: "rg32f",
        16: "rg32f", 17: "rg32u", 18: "rg32i", 19: "d32fs8", 20: "d32fs8", 21: "d32fs8", 22: "d32fs8", 23: "rgb10a2un",
        24: "rgb10a2un", 25: "rgb10a2u", 26: "r11g11b10f", 27: "rgba8", 28: "rgba8", 29: "rgba8", 30: "rgba8u",
        31: "rgba8sn", 32: "rgba8i", 33: "rg16f", 34: "rg16f", 35: "rg16un", 36: "rg16u", 37: "rg16sn", 38: "rg16i",
        39: "r32f", 40: "d32f", 41: "r32f", 42: "r32u", 43: "r32i", 44: "d24s8", 45: "d24s8", 46: "d24s8", 47: "d24s8",
        48: "rg8un", 49: "rg8un", 50: "rg8u", 51: "rg8sn", 52: "rg8i", 53: "r16un", 54: "r16f", 55: "d16", 56: "r16un",
        57: "r16u", 58: "r16sn", 59: "r16i", 60: "r8un", 61: "r8un", 62: "r8u", 63: "r8sn", 64: "r8i", 65: "a8un",
        67: "rgb9e5", 70: "bc1", 71: "bc1", 72: "bc1", 73: "bc2", 74: "bc2", 75: "bc2", 76: "bc3", 77: "bc3", 78: "bc3",
        79: "bc4", 80: "bc4", 81: "bc4s", 82: "bc5", 83: "bc5", 84: "bc5s", 85: "b5g6r5", 86: "bgr5a1", 87: "bgra8",
        88: "bgrx8", 90: "bgra8", 91: "bgra8", 92: "bgrx8", 93: "bgrx8", 94: "bc6h", 95: "bc6h", 96: "bc6hs", 97: "bc7",
        98: "bc7", 99: "bc7", 115: "bgra4"}
DXGI_DEPTH = {19, 20, 40, 44, 45, 55}         # formats that are depth buffers (as DSV formats)
DXGI_TYPELESS_DEPTH = {19, 39, 44, 53}        # typeless formats a depth buffer can be made of
# Direct3D 9 formats (D3DFORMAT), FOURCCs of the compressed ones
D3D9 = {21: "bgra8", 22: "bgrx8", 32: "rgba8", 33: "rgbx8", 23: "b5g6r5", 25: "bgr5a1", 24: "bgr5a1", 26: "bgra4",
        28: "a8un", 50: "l8", 51: "a8l8", 81: "l16", 36: "rgba16un", 113: "rgba16f", 116: "rgba32f", 114: "r32f",
        111: "r16f", 112: "rg16f", 115: "rg32f", 34: "rg16un", 31: "rgb10a2un", 35: "bgr10a2un", 60: "rg8sn",
        63: "rgba8sn", 71: "d32f", 75: "d24s8", 77: "d24s8", 79: "d24s8", 80: "d16", 82: "d32f", 83: "d24s8",
        0x31545844: "bc1", 0x32545844: "bc2", 0x33545844: "bc2", 0x34545844: "bc3", 0x35545844: "bc3",
        0x31495441: "bc4", 0x32495441: "bc5"}
D3D9_DEPTH = {71, 75, 77, 79, 80, 82, 83}
DXGI_SRGB = {29, 72, 75, 78, 91, 93, 99}


def to_linear(values):
    """sRGB -> linear (the r g b of (..., 4) values)."""
    out = numpy.array(values, dtype=numpy.float32, copy=True)
    c = numpy.clip(out[..., :3], 0, 1)
    out[..., :3] = numpy.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return out


def to_srgb(values):
    out = numpy.array(values, dtype=numpy.float32, copy=True)
    c = numpy.clip(out[..., :3], 0, 1)
    out[..., :3] = numpy.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)
    return out
EIGHT_BIT = {"rgba8", "bgra8", "bgrx8", "rgbx8"}   # kept as RGBA bytes


def kind_bytes(kind: str) -> int:
    if kind in SPECS:
        dtype, channels, _, _ = SPECS[kind]
        return numpy.dtype(dtype).itemsize * channels
    return PACKED.get(kind, BLOCKS.get(kind, 4))


def is_integer(kind: str) -> bool:
    return kind in SPECS and SPECS[kind][2] in ("uint", "sint") or kind == "rgb10a2u"


def pitch_of(kind: str, width: int) -> int:
    if kind in BLOCKS:
        return max(1, (width + 3) // 4) * BLOCKS[kind]
    return width * kind_bytes(kind)


def rows_of(kind: str, height: int) -> int:
    return max(1, (height + 3) // 4) if kind in BLOCKS else height


def decode(raw, kind: str, raw_integers=False):
    """Pixels of a non-compressed kind (n, bytes) -> (n, 4): floats (0..1 for unorm), or the integers as
    uint32 bit patterns with raw_integers for uint / sint kinds (what a shader reads)."""
    raw = numpy.ascontiguousarray(raw, dtype=numpy.uint8)
    n = raw.shape[0]
    out = numpy.zeros((n, 4), dtype=numpy.float64)
    out[:, 3] = 1
    if kind in SPECS:
        dtype, channels, cls, order = SPECS[kind]
        values = raw.view(numpy.dtype(dtype).newbyteorder("<")).reshape(n, channels).astype(numpy.float64)
        if cls in ("uint", "sint") and raw_integers:
            bits = numpy.zeros((n, 4), dtype=numpy.uint32)
            bits[:, 3] = 1
            ints = raw.view(numpy.dtype(dtype).newbyteorder("<")).reshape(n, channels).astype(numpy.int64)
            for i, c in enumerate(order):
                if c in "rgba":
                    bits[:, "rgba".index(c)] = (ints[:, i] & 0xFFFFFFFF).astype(numpy.uint32)
            return bits
        if cls == "unorm":
            values = values / float(numpy.iinfo(dtype).max)
        elif cls == "snorm":
            values = numpy.maximum(values / float(numpy.iinfo(dtype).max), -1)
        for i, c in enumerate(order):
            if c == "l":
                out[:, :3] = values[:, i:i + 1]
            elif c in "rgba":
                out[:, "rgba".index(c)] = values[:, i]
        if cls in ("float",) and "a" not in order:
            out[:, 3] = 1
        if cls == "depth":
            out[:, 1:] = 0
            out[:, 3] = 1
        return out
    v = raw.view("<u4").reshape(n) if kind_bytes(kind) == 4 and kind not in BLOCKS else None
    if kind in ("rgb10a2un", "rgb10a2u", "bgr10a2un"):
        scale = 1.0 if kind == "rgb10a2u" else None
        r, g, b, a = v & 1023, (v >> 10) & 1023, (v >> 20) & 1023, v >> 30
        if kind == "bgr10a2un":
            r, b = b, r
        if scale:
            return numpy.stack([r, g, b, a], 1).astype(numpy.uint32 if raw_integers else numpy.float64)
        out[:] = numpy.stack([r / 1023, g / 1023, b / 1023, a / 3], 1)
    elif kind == "r11g11b10f":
        out[:, 0], out[:, 1], out[:, 2] = _small_float(v & 0x7FF, 6), _small_float((v >> 11) & 0x7FF, 6), \
            _small_float(v >> 22, 5)
    elif kind == "rgb9e5":
        e = (v >> 27).astype(numpy.float64)
        scale = 2.0 ** (e - 15 - 9)
        out[:, 0], out[:, 1], out[:, 2] = (v & 511) * scale, ((v >> 9) & 511) * scale, ((v >> 18) & 511) * scale
    elif kind == "d24s8":
        out[:, 0] = (v & 0xFFFFFF) / 16777215.0
        out[:, 1] = v >> 24
    elif kind == "d32fs8":
        out[:, 0] = raw[:, :4].copy().view("<f4").reshape(n)
        out[:, 1] = raw[:, 4]
    else:
        h = raw.view("<u2").reshape(n)
        if kind == "b5g6r5":
            out[:, 0], out[:, 1], out[:, 2] = (h >> 11) / 31, ((h >> 5) & 63) / 63, (h & 31) / 31
        elif kind == "bgr5a1":
            out[:, 0], out[:, 1], out[:, 2], out[:, 3] = ((h >> 10) & 31) / 31, ((h >> 5) & 31) / 31, (h & 31) / 31, h >> 15
        elif kind == "bgra4":
            out[:, 0], out[:, 1], out[:, 2], out[:, 3] = ((h >> 8) & 15) / 15, ((h >> 4) & 15) / 15, (h & 15) / 15, \
                (h >> 12) / 15
    return out


def _small_float(bits, mantissa):
    exponent = (bits >> mantissa).astype(numpy.int64)
    m = (bits & ((1 << mantissa) - 1)).astype(numpy.float64)
    normal = (1 + m / (1 << mantissa)) * 2.0 ** (exponent - 15)
    denormal = m / (1 << mantissa) * 2.0 ** -14
    return numpy.where(exponent == 0, denormal, normal)


def encode(values, kind: str) -> numpy.ndarray:
    """(n, 4) values -> the pixels' bytes (n, bytes). Integers kinds take the integer values."""
    values = numpy.asarray(values)
    n = values.shape[0]
    if kind in SPECS:
        dtype, channels, cls, order = SPECS[kind]
        columns = []
        for c in order:
            if c == "x":
                columns.append(numpy.full(n, 1.0))
            elif c == "l":
                columns.append(values[:, 0])
            else:
                columns.append(values[:, "rgba".index(c)])
        data = numpy.stack(columns, 1).astype(numpy.float64)
        if cls == "unorm" or (cls == "depth" and dtype != "f4"):
            data = numpy.round(numpy.clip(data, 0, 1) * numpy.iinfo(dtype).max)
        elif cls == "snorm":
            data = numpy.round(numpy.clip(data, -1, 1) * numpy.iinfo(dtype).max)
        elif cls in ("uint", "sint"):
            info = numpy.iinfo(dtype)
            data = numpy.clip(data, info.min, info.max)
        return data.astype(numpy.dtype(dtype).newbyteorder("<")).view(numpy.uint8).reshape(n, -1)
    v = None
    if kind in ("rgb10a2un", "bgr10a2un"):
        q = numpy.round(numpy.clip(values, 0, 1) * [1023, 1023, 1023, 3]).astype(numpy.uint32)
        r, b = (q[:, 2], q[:, 0]) if kind == "bgr10a2un" else (q[:, 0], q[:, 2])
        v = r | (q[:, 1] << 10) | (b << 20) | (q[:, 3] << 30)
    elif kind == "rgb10a2u":
        q = numpy.clip(values, 0, [1023, 1023, 1023, 3]).astype(numpy.uint32)
        v = q[:, 0] | (q[:, 1] << 10) | (q[:, 2] << 20) | (q[:, 3] << 30)
    elif kind == "d24s8":
        v = numpy.round(numpy.clip(values[:, 0], 0, 1) * 16777215).astype(numpy.uint32) | \
            (numpy.clip(values[:, 1], 0, 255).astype(numpy.uint32) << 24)
    elif kind in ("r11g11b10f", "rgb9e5"):
        # rarely written by the CPU: half floats rounded into the format
        half = numpy.clip(values[:, :3], 0, 65000).astype(numpy.float16).view(numpy.uint16).astype(numpy.uint32)
        r, g, b = (half[:, 0] >> 4) & 0x7FF, (half[:, 1] >> 4) & 0x7FF, (half[:, 2] >> 5) & 0x3FF
        v = r | (g << 11) | (b << 22)
    if v is not None:
        return v.astype("<u4").view(numpy.uint8).reshape(n, 4)
    if kind == "d32fs8":
        out = numpy.zeros((n, 8), dtype=numpy.uint8)
        out[:, :4] = values[:, 0].astype("<f4").view(numpy.uint8).reshape(n, 4)
        out[:, 4] = numpy.clip(values[:, 1], 0, 255).astype(numpy.uint8)
        return out
    q = numpy.clip(values, 0, 1)
    if kind == "b5g6r5":
        h = (numpy.round(q[:, 0] * 31).astype(numpy.uint16) << 11) | (numpy.round(q[:, 1] * 63).astype(numpy.uint16) << 5) | \
            numpy.round(q[:, 2] * 31).astype(numpy.uint16)
    elif kind == "bgr5a1":
        h = (numpy.round(q[:, 3]).astype(numpy.uint16) << 15) | (numpy.round(q[:, 0] * 31).astype(numpy.uint16) << 10) | \
            (numpy.round(q[:, 1] * 31).astype(numpy.uint16) << 5) | numpy.round(q[:, 2] * 31).astype(numpy.uint16)
    else:
        h = (numpy.round(q[:, 3] * 15).astype(numpy.uint16) << 12) | (numpy.round(q[:, 0] * 15).astype(numpy.uint16) << 8) | \
            (numpy.round(q[:, 1] * 15).astype(numpy.uint16) << 4) | numpy.round(q[:, 2] * 15).astype(numpy.uint16)
    return h.astype("<u2").view(numpy.uint8).reshape(n, 2)


# ------------------------------------------------------------------ block compression
def _rgb565(c):
    c = c.astype(numpy.uint32)
    return numpy.stack([((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31], -1).astype(numpy.float64)


def _bc1_colors(block, four_colors):
    c0, c1 = block[:, 0:2].copy().view("<u2")[:, 0], block[:, 2:4].copy().view("<u2")[:, 0]
    a, b = _rgb565(c0), _rgb565(c1)
    three = (~four_colors) & (c0 <= c1)
    palette = numpy.zeros((len(block), 4, 4))
    palette[:, 0, :3], palette[:, 1, :3] = a, b
    palette[:, 2, :3] = numpy.where(three[:, None], (a + b) / 2, (2 * a + b) / 3)
    palette[:, 3, :3] = numpy.where(three[:, None], 0, (a + 2 * b) / 3)
    palette[:, :, 3] = 255
    palette[:, 3, 3] = numpy.where(three, 0, 255)
    bits = block[:, 4:8].copy().view("<u4")[:, 0]
    index = (bits[:, None] >> (2 * numpy.arange(16))) & 3
    return numpy.take_along_axis(palette, index[:, :, None].astype(numpy.int64).repeat(4, 2), 1)  # (n, 16, 4)


def _alpha_block(block, signed=False):
    a0, a1 = block[:, 0].astype(numpy.float64), block[:, 1].astype(numpy.float64)
    if signed:
        a0, a1 = block[:, 0:1].view(numpy.int8)[:, 0].astype(numpy.float64), block[:, 1:2].view(numpy.int8)[:, 0].astype(numpy.float64)
    palette = numpy.zeros((len(block), 8))
    palette[:, 0], palette[:, 1] = a0, a1
    eight = a0 > a1
    for i in range(1, 7):
        palette[:, i + 1] = numpy.where(eight, ((7 - i) * a0 + i * a1) / 7, 0)
    for i in range(1, 5):
        palette[:, i + 1] = numpy.where(eight, palette[:, i + 1], ((5 - i) * a0 + i * a1) / 5)
    low, high = (-127.0, 127.0) if signed else (0.0, 255.0)
    palette[:, 6] = numpy.where(eight, palette[:, 6], low)
    palette[:, 7] = numpy.where(eight, palette[:, 7], high)
    bits = numpy.zeros(len(block), dtype=numpy.uint64)
    for i in range(6):
        bits |= block[:, 2 + i].astype(numpy.uint64) << numpy.uint64(8 * i)
    index = ((bits[:, None] >> (numpy.uint64(3) * numpy.arange(16, dtype=numpy.uint64))) & numpy.uint64(7)).astype(numpy.int64)
    values = numpy.take_along_axis(palette, index, 1)
    return values / (127.0 if signed else 255.0)


def decode_blocks(data: bytes, kind: str, width: int, height: int):
    """A block-compressed image -> (h, w, 4) float32."""
    bw, bh = max(1, (width + 3) // 4), max(1, (height + 3) // 4)
    size = BLOCKS[kind]
    raw = numpy.frombuffer(bytes(data[:bw * bh * size]).ljust(bw * bh * size, b"\0"), dtype=numpy.uint8).reshape(-1, size)
    texels = numpy.zeros((len(raw), 16, 4))
    texels[:, :, 3] = 1
    if kind == "bc1":
        texels = _bc1_colors(raw, numpy.zeros(len(raw), bool)) / 255
    elif kind == "bc2":
        texels = _bc1_colors(raw[:, 8:], numpy.ones(len(raw), bool)) / 255
        alpha = numpy.zeros((len(raw), 16))
        for i in range(8):
            alpha[:, 2 * i] = (raw[:, i] & 15) / 15
            alpha[:, 2 * i + 1] = (raw[:, i] >> 4) / 15
        texels[:, :, 3] = alpha
    elif kind == "bc3":
        texels = _bc1_colors(raw[:, 8:], numpy.ones(len(raw), bool)) / 255
        texels[:, :, 3] = _alpha_block(raw[:, :8])
    elif kind in ("bc4", "bc4s"):
        texels[:, :, 0] = _alpha_block(raw, kind == "bc4s")
    elif kind in ("bc5", "bc5s"):
        texels[:, :, 0] = _alpha_block(raw[:, :8], kind == "bc5s")
        texels[:, :, 1] = _alpha_block(raw[:, 8:], kind == "bc5s")
    elif kind == "bc7":
        from .bc67 import decode_bc7
        texels = decode_bc7(raw)
    elif kind in ("bc6h", "bc6hs"):
        from .bc67 import decode_bc6h
        texels = decode_bc6h(raw, kind == "bc6hs")
    image = texels.reshape(bh, bw, 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4)
    return image[:height, :width].astype(numpy.float32)


# ------------------------------------------------------------------ images
class Image:
    """A 2D image of a kind: RGBA bytes (fb.color, the 8-bit kinds) or (h, w, 4) float32 (data), a depth
    buffer (fb.depth) for the depth kinds and the render targets that have one, stencil ignored."""

    def __init__(self, width: int, height: int, kind: str = "bgra8"):
        self.kind = kind
        self.fb = Framebuffer(width, height)
        self.width, self.height = self.fb.width, self.fb.height
        self.data = None if kind in EIGHT_BIT else numpy.zeros((self.height, self.width, 4), dtype=numpy.float32)
        if kind in EIGHT_BIT:
            self.fb.color[...] = 0
        if self.data is not None:
            self.data[..., 3] = 0 if kind in SPECS and SPECS[kind][2] in ("uint", "sint") else 1
        self.raw = None             # block-compressed: the program's bytes
        self.mapped: Optional[int] = None
        self.mapped_size = 0
        self._texels = None

    @property
    def is_depth(self) -> bool:
        return self.kind in ("d32f", "d16", "d24s8", "d32fs8")

    @property
    def color(self):
        """The array the rasterizer writes (uint8 or float32)."""
        return self.fb.color if self.data is None else self.data

    @property
    def pitch(self) -> int:
        return pitch_of(self.kind, self.width)

    def changed(self):
        self._texels = None

    def to_bytes(self) -> bytes:
        if self.raw is not None:
            return bytes(self.raw)
        if self.is_depth:
            values = numpy.zeros((self.width * self.height, 4))
            values[:, 0] = self.fb.depth.reshape(-1)
            return encode(values, self.kind).tobytes()
        if self.data is None:
            color = self.fb.color
            if self.kind in ("bgra8", "bgrx8"):
                color = color[..., [2, 1, 0, 3]]
            if self.kind in ("bgrx8", "rgbx8"):
                color = color.copy()
                color[..., 3] = 255
            return numpy.ascontiguousarray(color).tobytes()
        return encode(self.data.reshape(-1, 4), self.kind).tobytes()

    def from_bytes(self, data: bytes, pitch: Optional[int] = None, rect=None):
        """The program's bytes (rows of pitch bytes) into the image, or into rect (x0, y0, x1, y1)."""
        self._texels = None
        if self.kind in BLOCKS:
            if rect is None or rect == (0, 0, self.width, self.height):
                row = self.pitch
                pitch = pitch or row
                rows = rows_of(self.kind, self.height)
                lines = [bytes(data[i * pitch:i * pitch + row]).ljust(row, b"\0") for i in range(rows)]
                self.raw = bytearray(b"".join(lines))
                self.data[...] = decode_blocks(self.raw, self.kind, self.width, self.height)
            return
        x0, y0, x1, y1 = rect if rect is not None else (0, 0, self.width, self.height)
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(self.width, x1), min(self.height, y1)
        if x0 >= x1 or y0 >= y1:
            return
        bpp = kind_bytes(self.kind)
        pitch = pitch or (x1 - x0) * bpp
        rows = y1 - y0
        need = pitch * (rows - 1) + (x1 - x0) * bpp
        buffer = numpy.frombuffer(bytes(data[:pitch * rows]).ljust(max(need, pitch * rows), b"\0"), dtype=numpy.uint8)
        lines = numpy.lib.stride_tricks.as_strided(buffer, (rows, (x1 - x0) * bpp), (pitch, 1))
        pixels = numpy.ascontiguousarray(lines).reshape(-1, bpp)
        if self.is_depth:
            self.fb.depth[y0:y1, x0:x1] = decode(pixels, self.kind)[:, 0].reshape(rows, x1 - x0)
            return
        if self.data is None:
            block = pixels.reshape(rows, x1 - x0, 4)
            if self.kind in ("bgra8", "bgrx8"):
                block = block[..., [2, 1, 0, 3]]
            self.fb.color[y0:y1, x0:x1] = block
            if self.kind in ("bgrx8", "rgbx8"):
                self.fb.color[y0:y1, x0:x1, 3] = 255
            return
        self.data[y0:y1, x0:x1] = decode(pixels, self.kind, raw_integers=False).reshape(rows, x1 - x0, 4)

    def texels(self):
        """(h, w, 4) float32 for sampling (0..1 for the normalized kinds)."""
        if self._texels is None:
            if self.is_depth:
                texels = numpy.zeros((self.height, self.width, 4), dtype=numpy.float32)
                texels[..., 0] = self.fb.depth
                texels[..., 3] = 1
            elif self.data is None:
                texels = self.fb.color.astype(numpy.float32) / 255
                if self.kind in ("bgrx8", "rgbx8"):
                    texels[..., 3] = 1
            else:
                texels = self.data
            self._texels = texels
        return self._texels

    def set_texels(self, values):
        """(h, w, 4) values written by a shader (an unordered access view) back into the image."""
        self._texels = None
        if self.is_depth:
            self.fb.depth[...] = values[..., 0]
        elif self.data is None:
            self.fb.color[...] = numpy.round(numpy.clip(values, 0, 1) * 255).astype(numpy.uint8)
        else:
            self.data[...] = values

    def clear(self, rgba, rect=None):
        """Fills the image (or rect x0, y0, x1, y1) with a color."""
        self._texels = None
        if self.data is None:
            self.fb.clear(rgba, None, rect)
            return
        x0, y0, x1, y1 = rect if rect is not None else (0, 0, self.width, self.height)
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(self.width, x1), min(self.height, y1)
        if x0 < x1 and y0 < y1:
            self.data[y0:y1, x0:x1] = numpy.asarray(rgba, dtype=numpy.float32)

    def copy_from(self, other: "Image", src=None, dst=(0, 0)):
        self._texels = None
        x0, y0, x1, y1 = src if src is not None else (0, 0, other.width, other.height)
        dx, dy = dst
        w, h = min(x1 - x0, self.width - dx), min(y1 - y0, self.height - dy)
        if w <= 0 or h <= 0:
            return
        if self.raw is not None or other.raw is not None:
            if other.raw is not None and (src is None or src == (0, 0, other.width, other.height)):
                self.raw = bytearray(other.raw)
            if self.data is not None and other.data is not None:
                self.data[dy:dy + h, dx:dx + w] = other.data[y0:y0 + h, x0:x0 + w]
            return
        if self.is_depth or other.is_depth:
            self.fb.depth[dy:dy + h, dx:dx + w] = other.fb.depth[y0:y0 + h, x0:x0 + w]
            return
        if self.data is None and other.data is None:
            self.fb.color[dy:dy + h, dx:dx + w] = other.fb.color[y0:y0 + h, x0:x0 + w]
        else:
            part = other.texels()[y0:y0 + h, x0:x0 + w]
            if self.data is None:
                self.fb.color[dy:dy + h, dx:dx + w] = numpy.round(numpy.clip(part, 0, 1) * 255).astype(numpy.uint8)
            else:
                self.data[dy:dy + h, dx:dx + w] = part
        self.fb.depth[dy:dy + h, dx:dx + w] = other.fb.depth[y0:y0 + h, x0:x0 + w]

    def map(self, dp) -> int:
        """The image in the emulator's memory (Lock / Map)."""
        data = self.to_bytes()
        if self.mapped is None or self.mapped_size < len(data):
            self.mapped = dp.allocate(max(len(data), 16), page_align=True)
            self.mapped_size = len(data)
        dp.write(self.mapped, data)
        return self.mapped

    def unmap(self, dp):
        if self.mapped is not None:
            size = self.pitch * rows_of(self.kind, self.height)
            self.from_bytes(bytes(dp.read(self.mapped, size)), self.pitch)

    def picture(self):
        """A Pillow RGB image of the pixels (what a Present shows)."""
        from PIL import Image as PilImage
        if self.data is None:
            return PilImage.fromarray(self.fb.color, "RGBA").convert("RGB")
        rgb = numpy.round(numpy.clip(numpy.nan_to_num(self.data[..., :3]), 0, 1) * 255).astype(numpy.uint8)
        return PilImage.fromarray(numpy.ascontiguousarray(rgb), "RGB")


# ------------------------------------------------------------------ views and samplers (what shaders use)
class Sampler:
    def __init__(self, linear=False, address=("clamp", "clamp", "clamp"), border=(0.0, 0.0, 0.0, 0.0), compare=0,
                 mip_linear=False, min_lod=0.0, max_lod=1e9, lod_bias=0.0):
        self.linear, self.address, self.border, self.compare = linear, address, border, compare
        self.mip_linear, self.min_lod, self.max_lod, self.lod_bias = mip_linear, min_lod, max_lod, lod_bias

    @staticmethod
    def from_d3d11(data: bytes) -> "Sampler":
        import struct
        filter_type, u, v, w = struct.unpack_from("<4I", data, 0)
        bias = struct.unpack_from("<f", data, 16)[0]
        compare = struct.unpack_from("<I", data, 24)[0]
        border = struct.unpack_from("<4f", data, 28)
        min_lod, max_lod = struct.unpack_from("<2f", data, 44)
        modes = {1: "wrap", 2: "mirror", 3: "clamp", 4: "border", 5: "mirror"}
        return Sampler(bool(filter_type & 0x4) or filter_type & 0x40 != 0, tuple(modes.get(m, "clamp") for m in (u, v, w)),
                       border, compare if filter_type & 0x80 else 0, bool(filter_type & 0x1), min_lod, max_lod, bias)


DEFAULT_SAMPLER = Sampler()


class Resource:
    """What a view sees of a resource: images[slice][mip] (textures), or bytes (buffers)."""

    def __init__(self, kind: str, images: Optional[List[List[Image]]] = None, data=None, dimension="2d"):
        self.kind, self.images, self.data, self.dimension = kind, images or [], data, dimension


class View:
    """A shader resource view or an unordered access view.
    kind: texture (images, slices first_slice..), buffer (typed: element format), raw, structured (stride)."""

    def __init__(self, kind: str, images: Optional[List[List[Image]]] = None, buffer=None, fmt: str = "rgba32f",
                 first: int = 0, count: int = 0, stride: int = 0, dimension: str = "2d", mip: int = 0, mips: int = 1,
                 counter: Optional[list] = None):
        self.kind, self.images, self.buffer, self.fmt = kind, images or [], buffer, fmt
        self.first, self.count, self.stride, self.dimension = first, count, stride, dimension
        self.mip, self.mips = mip, mips
        self.counter = counter          # [value] of an append / consume buffer
        self.srgb = False
        self.bytes = None               # the buffer's bytes while a shader runs (numpy uint8)
        self.dirty = False

    def level(self, slice_index: int = 0, mip: int = 0) -> Optional[Image]:
        if not self.images:
            return None
        s = min(max(slice_index, 0), len(self.images) - 1)
        levels = self.images[s]
        return levels[min(max(self.mip + mip, 0), len(levels) - 1)] if levels else None


def view_levels(view: View, slice_index: int = 0) -> List[Image]:
    """The mip chain a view sees of one of its slices."""
    if not view.images:
        return []
    levels = view.images[min(max(slice_index, 0), len(view.images) - 1)][view.mip:]
    return levels[:view.mips] if view.mips and view.mips > 0 else levels


def _sizes(view: View, image: Image):
    if view.dimension == "cube":
        return (image.width, image.height)
    if view.dimension in ("1d", "1darray"):
        return (image.width,)
    if view.dimension == "3d":
        return (image.width, image.height, len(view.images))
    return (image.width, image.height)


def view_lod(view: View, sampler: Sampler, coords, lod=None, bias=None, gradients=None):
    """The mip level of each lane: explicit (lod), from gradients (ddx, ddy of the coordinates) or from the
    derivatives of the coordinates in a fragment stage running in quads; with the sampler's bias and
    clamps. None: the first level for all."""
    from .raster import implicit_lod, lod_of_gradients
    base = view.level(0, 0)
    if base is None:
        return None
    sizes = _sizes(view, base)
    if lod is None:
        coords = numpy.asarray(coords, dtype=numpy.float64)
        if view.dimension == "cube":
            # the derivatives of the face coordinates: the direction divided by its longest component
            major = numpy.abs(coords[:, :3]).max(axis=1, keepdims=True)
            coords = coords[:, :3] / numpy.where(major == 0, 1, major) * 0.5
            sizes = (base.width, base.width, base.width)
        k = len(sizes)
        if gradients is not None:
            gx, gy = (numpy.asarray(g, dtype=numpy.float64)[:, :k] * numpy.asarray(sizes[:k]) for g in gradients)
            if view.dimension == "cube":
                major = numpy.abs(numpy.asarray(coords)[:, :3]).max(axis=1, keepdims=True)
                gx, gy = gx / numpy.where(major == 0, 1, major) * 0.5, gy / numpy.where(major == 0, 1, major) * 0.5
            lod = lod_of_gradients(gx, gy)
        else:
            lod = implicit_lod(coords[:, :k], sizes)
            if lod is None:
                return None
    lod = numpy.asarray(lod, dtype=numpy.float64) + sampler.lod_bias + (0 if bias is None else numpy.asarray(bias))
    return numpy.clip(lod, sampler.min_lod, sampler.max_lod)


def sample_view(view: View, sampler: Sampler, coords, lod=None, compare_to=None, offset=(0, 0), bias=None,
                gradients=None):
    """Sampling of a texture view at coords (n, k) floats: (n, 4) float32. The mip level: lod (n,), the
    gradients (ddx, ddy), or the derivatives of the coordinates in a fragment stage running in quads."""
    from .raster import sample_mips
    coords = numpy.asarray(coords, dtype=numpy.float64)
    n = coords.shape[0]
    if view is None or not view.images:
        return numpy.zeros((n, 4), dtype=numpy.float32)
    levels_of_lane = view_lod(view, sampler, coords, lod, bias, gradients)
    if view.dimension == "cube":
        out = _sample_cube(view, sampler, coords, levels_of_lane)
    else:
        if view.dimension in ("2darray", "1darray"):
            layer_axis = 2 if view.dimension == "2darray" else 1
            layers = numpy.clip(numpy.round(coords[:, layer_axis]).astype(numpy.int64), 0, len(view.images) - 1)
        elif view.dimension == "3d" and coords.shape[1] > 2:  # depth slices: the nearest
            layers = numpy.clip(numpy.floor(coords[:, 2] * len(view.images)).astype(numpy.int64), 0, len(view.images) - 1)
        else:
            layers = numpy.zeros(n, dtype=numpy.int64)
        out = numpy.zeros((n, 4), dtype=numpy.float32)
        v_coord = coords[:, 1] if coords.shape[1] > 1 and view.dimension not in ("1d", "1darray") else numpy.full(n, 0.5)
        address = sampler.address[:2]
        border = sampler.border if "border" in address else None
        for layer in numpy.unique(layers):
            sel = numpy.nonzero(layers == layer)[0]
            levels = view_levels(view, int(layer))
            if not levels:
                continue
            arrays = [image.texels() for image in levels]
            u = coords[sel, 0] + offset[0] / levels[0].width
            v = v_coord[sel] + offset[1] / levels[0].height
            lods = None if levels_of_lane is None else levels_of_lane[sel]
            out[sel] = sample_mips(arrays, u, v, lods, sampler.linear, sampler.mip_linear, address, border)
    if view.srgb:
        out = to_linear(out)
    if compare_to is not None:
        from .raster import DEPTH_TESTS
        test = DEPTH_TESTS.get(sampler.compare or 4, numpy.less_equal)
        passed = test(numpy.asarray(compare_to, dtype=numpy.float64), out[:, 0].astype(numpy.float64)).astype(numpy.float32)
        out = numpy.repeat(passed[:, None], 4, axis=1)
    return out


def _sample_cube(view, sampler, coords, lod):
    from .raster import sample_mips
    n = coords.shape[0]
    x, y, z = coords[:, 0], coords[:, 1], coords[:, 2]
    ax, ay, az = numpy.abs(x), numpy.abs(y), numpy.abs(z)
    face = numpy.where((ax >= ay) & (ax >= az), numpy.where(x >= 0, 0, 1),
                       numpy.where(ay >= az, numpy.where(y >= 0, 2, 3), numpy.where(z >= 0, 4, 5)))
    major = numpy.where(face < 2, ax, numpy.where(face < 4, ay, az))
    major = numpy.where(major == 0, 1, major)
    sc = numpy.select([face == 0, face == 1, face == 2, face == 3, face == 4], [-z, z, x, x, x], -x)
    tc = numpy.select([face == 2, face == 3], [z, -z], -y)
    u, v = (sc / major + 1) / 2, (tc / major + 1) / 2
    out = numpy.zeros((n, 4), dtype=numpy.float32)
    layer = numpy.clip(numpy.floor(coords[:, 3]).astype(numpy.int64), 0, max(len(view.images) // 6 - 1, 0)) \
        if coords.shape[1] > 3 and view.dimension == "cube" and len(view.images) > 6 else numpy.zeros(n, dtype=numpy.int64)
    for f in range(6):
        for l in numpy.unique(layer):
            sel = numpy.nonzero((face == f) & (layer == l))[0]
            levels = view_levels(view, int(l) * 6 + f)
            if len(sel) and levels:
                out[sel] = sample_mips([image.texels() for image in levels], u[sel], v[sel],
                                       None if lod is None else lod[sel], sampler.linear, sampler.mip_linear, "clamp")
    return out


def load_view(view: View, coords_int, mip=0):
    """ld of a texture / typed buffer view: (n, 4) float32 values (integers formats: float values)."""
    n = coords_int.shape[0]
    out = numpy.zeros((n, 4), dtype=numpy.float32)
    if view is None:
        return out
    if view.kind == "buffer":
        data = view.bytes if view.bytes is not None else numpy.zeros(0, dtype=numpy.uint8)
        size = kind_bytes(view.fmt)
        index = coords_int[:, 0].astype(numpy.int64) + view.first
        ok = (index >= 0) & ((index + 1) * size <= len(data)) & (coords_int[:, 0] < (view.count or 1 << 62))
        if ok.any():
            starts = index[ok] * size
            raw = data[starts[:, None] + numpy.arange(size)]
            out[ok] = decode(raw, view.fmt)
        return out
    layer_axis = {"2darray": 2, "1darray": 1, "3d": 2}.get(view.dimension)
    layers = coords_int[:, layer_axis].astype(numpy.int64) if layer_axis is not None and coords_int.shape[1] > layer_axis \
        else numpy.zeros(n, dtype=numpy.int64)
    for layer in numpy.unique(layers):
        image = view.level(int(layer), int(mip))
        if image is None:
            continue
        sel = numpy.nonzero(layers == layer)[0]
        texels = image.texels()
        x = coords_int[sel, 0].astype(numpy.int64)
        y = coords_int[sel, 1].astype(numpy.int64) if coords_int.shape[1] > 1 and view.dimension not in ("1d", "1darray") \
            else numpy.zeros(len(sel), dtype=numpy.int64)
        inside = (x >= 0) & (x < image.width) & (y >= 0) & (y < image.height)
        values = texels[numpy.clip(y, 0, image.height - 1), numpy.clip(x, 0, image.width - 1)]
        out[sel] = values * inside[:, None]
    return to_linear(out) if view.srgb else out


def store_view(view: View, coords_int, values, lanes):
    """store_uav_typed: values (n, 4) at coords for the lanes (in order: a later lane wins)."""
    if view is None or len(lanes) == 0:
        return
    if view.kind == "buffer":
        size = kind_bytes(view.fmt)
        index = coords_int[lanes, 0].astype(numpy.int64) + view.first
        ok = (index >= 0) & ((index + 1) * size <= len(view.bytes))
        if ok.any():
            encoded = encode(values[lanes][ok], view.fmt)
            starts = index[ok] * size
            view.bytes[starts[:, None] + numpy.arange(size)] = encoded
            view.dirty = True
        return
    layer_axis = {"2darray": 2, "1darray": 1, "3d": 2}.get(view.dimension)
    layers = coords_int[lanes, layer_axis].astype(numpy.int64) if layer_axis is not None else numpy.zeros(len(lanes), numpy.int64)
    for layer in numpy.unique(layers):
        image = view.level(int(layer), 0)
        if image is None:
            continue
        sel = lanes[layers == layer]
        texels = image.texels().copy()
        x = coords_int[sel, 0].astype(numpy.int64)
        y = coords_int[sel, 1].astype(numpy.int64) if coords_int.shape[1] > 1 and view.dimension not in ("1d", "1darray") \
            else numpy.zeros(len(sel), dtype=numpy.int64)
        inside = (x >= 0) & (x < image.width) & (y >= 0) & (y < image.height)
        texels[y[inside], x[inside]] = values[sel][inside]
        image.set_texels(texels)


# ------------------------------------------------------------------ vertex attributes
def fetch_attribute(data: bytes, offset: int, stride: int, rows, kind: str, integers_as_bits=True):
    """An attribute of the vertices rows of a vertex buffer: (n, 4), floats as float32 bit patterns
    (uint32) for a shader's input registers, or the integers' bits for uint / sint kinds."""
    rows = numpy.asarray(rows, dtype=numpy.int64)
    n = len(rows)
    size = kind_bytes(kind)
    buffer = numpy.frombuffer(data, dtype=numpy.uint8) if not isinstance(data, numpy.ndarray) else data
    starts = offset + rows * stride
    valid = (starts >= 0) & (starts + size <= len(buffer))
    raw = numpy.zeros((n, size), dtype=numpy.uint8)
    if valid.any():
        raw[valid] = buffer[starts[valid][:, None] + numpy.arange(size)]
    if is_integer(kind):
        bits = decode(raw, kind, raw_integers=True)
        return bits if integers_as_bits else bits.astype(numpy.float64)
    values = decode(raw, kind)
    if kind in SPECS and SPECS[kind][1] < 4 and SPECS[kind][2] in ("float", "unorm", "snorm"):
        values[:, SPECS[kind][1]:3] = 0
        values[:, 3] = 1
    return values.astype(numpy.float32).view(numpy.uint32) if integers_as_bits else values
