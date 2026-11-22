# Fathom addition to dumpulator: Direct3D 9, Direct3D 11 and DXGI for the emulated program, drawn by
# raster.py (and dxbc.py for the shaders of Direct3D 11). Boost Software License 1.0, like the rest of
# dumpulator (LICENSE.dumpulator).
#
# d3d9.dll, d3d11.dll and dxgi.dll need a graphics driver the emulator does not have: their entry points
# (Direct3DCreate9, D3D11CreateDevice, D3D11CreateDeviceAndSwapChain, CreateDXGIFactory / 1 / 2) give
# COM objects implemented here (apihooks.ComRuntime: a vtable per interface, a Python method per
# method). Direct3D 9: the fixed-function pipeline (FVF or vertex declarations, world / view /
# projection transforms, viewport, culling, depth test, alpha blending, flat / Gouraud shading,
# directional and point lights, a texture at stage 0 modulated by the color), DrawPrimitive(UP),
# DrawIndexedPrimitive(UP), vertex / index buffers, surfaces, textures, Clear, ColorFill, StretchRect,
# UpdateSurface, GetRenderTargetData, Present to the window; the vertex / pixel shaders of Direct3D 9 are
# not run (the draw is skipped and named). Direct3D 11: buffers, 2D textures, views, the input layout,
# vertex and pixel shaders run by dxbc.py, constant buffers, samplers, the rasterizer / blend /
# depth-stencil states, viewports, Draw(Indexed)(Instanced), ClearRenderTargetView,
# ClearDepthStencilView, CopyResource, CopySubresourceRegion, UpdateSubresource, Map / Unmap, the swap
# chain (GetBuffer, Present, ResizeBuffers). The surfaces are 32 bits per pixel (8 bits per channel);
# what a Present shows goes to the window's image (gui_draw.py).

import struct
from typing import Dict, List, Optional

from .apihooks import com_runtime, read_guid, E_NOTIMPL, E_NOINTERFACE
from .graphics_tables import IIDS
from .raster import Framebuffer, numpy, primitives, sample

E_INVALIDARG, E_OUTOFMEMORY, E_FAIL = 0x80070057, 0x8007000E, 0x80004005
D3DERR_NOTAVAILABLE, D3DERR_INVALIDCALL = 0x8876086A, 0x8876086C
DXGI_ERROR_NOT_FOUND, DXGI_ERROR_INVALID_CALL = 0x887A0002, 0x887A0001
S_FALSE = 1

# formats with 4 bytes per pixel, R G B A in memory (else B G R A); X: no alpha
D3D9_RGBA = {32: (True, False), 33: (True, True)}
D3D9_BGRA = {21: (False, False), 22: (False, True)}
DXGI_RGBA = {27: (True, False), 28: (True, False), 29: (True, False), 30: (True, False)}
DXGI_BGRA = {87: (False, False), 88: (False, True), 90: (False, False), 91: (False, False), 92: (False, True),
             93: (False, True)}
DXGI_DEPTH = {19, 20, 39, 40, 44, 45, 55, 53}


def _missing(dp, key: str):
    missing = dp._fathom_unimplemented
    if key not in missing:
        dp.info(f"{key} (not drawn)")
    missing[key] = missing.get(key, 0) + 1


# ------------------------------------------------------------------ images
class Image:
    """A 2D image: a framebuffer (RGBA bytes + depth) and its format in memory."""

    def __init__(self, width: int, height: int, rgba_order=False, no_alpha=False, fmt=0, raw_bpp=0):
        self.fb = Framebuffer(width, height)
        self.width, self.height = self.fb.width, self.fb.height
        self.rgba_order, self.no_alpha, self.fmt = rgba_order, no_alpha, fmt
        self.raw = None          # other formats: the bytes as the program wrote them
        if raw_bpp:
            self.raw = numpy.zeros((self.height, self.width * raw_bpp), dtype=numpy.uint8)
        self.mapped: Optional[int] = None
        self.mapped_size = 0

    @property
    def pitch(self) -> int:
        return self.raw.shape[1] if self.raw is not None else self.width * 4

    def to_bytes(self) -> bytes:
        if self.raw is not None:
            return self.raw.tobytes()
        color = self.fb.color if self.rgba_order else self.fb.color[..., [2, 1, 0, 3]]
        return numpy.ascontiguousarray(color).tobytes()

    def from_bytes(self, data: bytes, pitch: Optional[int] = None, rect=None):
        pitch = pitch or self.pitch
        x0, y0, x1, y1 = rect if rect is not None else (0, 0, self.width, self.height)
        rows = y1 - y0
        bpp = self.pitch // self.width
        if len(data) < pitch * (rows - 1) + (x1 - x0) * bpp:
            return
        lines = numpy.frombuffer(bytes(data[:pitch * rows]).ljust(pitch * rows, b"\0"), dtype=numpy.uint8).reshape(rows, pitch)
        lines = lines[:, :(x1 - x0) * bpp]
        if self.raw is not None:
            self.raw[y0:y1, x0 * bpp:x1 * bpp] = lines
            return
        pixels = lines.reshape(rows, x1 - x0, 4)
        self.fb.color[y0:y1, x0:x1] = pixels if self.rgba_order else pixels[..., [2, 1, 0, 3]]
        if self.no_alpha:
            self.fb.color[y0:y1, x0:x1, 3] = 255

    def texels(self):
        """The image as (h, w, 4) floats 0..1 (for sampling)."""
        if self.raw is not None:
            return _decode_raw(self.raw, self.fmt, self.width, self.height)
        texels = self.fb.color.astype(numpy.float32) / 255
        if self.no_alpha:
            texels[..., 3] = 1
        return texels

    def copy_from(self, other: "Image", src=None, dst=(0, 0)):
        x0, y0, x1, y1 = src if src is not None else (0, 0, other.width, other.height)
        dx, dy = dst
        w, h = min(x1 - x0, self.width - dx), min(y1 - y0, self.height - dy)
        if w <= 0 or h <= 0:
            return
        if self.raw is not None and other.raw is not None:
            bpp = self.pitch // self.width
            self.raw[dy:dy + h, dx * bpp:(dx + w) * bpp] = other.raw[y0:y0 + h, x0 * bpp:(x0 + w) * bpp]
        else:
            self.fb.color[dy:dy + h, dx:dx + w] = other.fb.color[y0:y0 + h, x0:x0 + w]
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
            self.from_bytes(bytes(dp.read(self.mapped, self.pitch * self.height)))


RAW_BPP = {2: 16, 10: 8, 11: 8, 16: 8, 24: 4, 34: 4, 35: 4, 41: 4, 42: 4, 43: 4, 49: 2, 54: 2, 56: 2, 61: 1, 62: 1, 65: 1, 6: 12}


def _decode_raw(raw, fmt, width, height):
    out = numpy.zeros((height, width, 4), dtype=numpy.float32)
    out[..., 3] = 1
    flat = numpy.ascontiguousarray(raw)
    if fmt == 2:
        out[:] = flat.view(numpy.float32).reshape(height, width, 4)
    elif fmt == 6:
        out[..., :3] = flat.view(numpy.float32).reshape(height, width, 3)
    elif fmt == 10:
        out[:] = flat.view(numpy.float16).reshape(height, width, 4).astype(numpy.float32)
    elif fmt == 11:
        out[:] = flat.view(numpy.uint16).reshape(height, width, 4) / 65535
    elif fmt == 16:
        out[..., :2] = flat.view(numpy.float32).reshape(height, width, 2)
    elif fmt == 24:
        v = flat.view(numpy.uint32).reshape(height, width)
        out[..., 0], out[..., 1], out[..., 2] = (v & 1023) / 1023, ((v >> 10) & 1023) / 1023, ((v >> 20) & 1023) / 1023
        out[..., 3] = (v >> 30) / 3
    elif fmt in (41, 43):
        out[..., 0] = flat.view(numpy.float32).reshape(height, width)
    elif fmt == 49:
        out[..., :2] = flat.reshape(height, width, 2) / 255
    elif fmt in (61, 62):
        out[..., 0] = flat.reshape(height, width) / 255
    elif fmt == 65:
        out[..., :3] = 0
        out[..., 3] = flat.reshape(height, width) / 255
    elif fmt == 54:
        out[..., 0] = flat.view(numpy.float16).reshape(height, width).astype(numpy.float32)
    elif fmt == 56:
        out[..., 0] = flat.view(numpy.uint16).reshape(height, width) / 65535
    elif fmt == 34:
        out[..., :2] = flat.view(numpy.float16).reshape(height, width, 2).astype(numpy.float32)
    elif fmt == 35:
        out[..., :2] = flat.view(numpy.uint16).reshape(height, width, 2) / 65535
    return out


# ------------------------------------------------------------------ output merger
def _factor(code, src, dst, constant):
    """A blend factor as (..., 4) (Direct3D 9 and 11 number them alike)."""
    a_s, a_d = numpy.repeat(src[..., 3:4], 4, axis=-1), numpy.repeat(dst[..., 3:4], 4, axis=-1)
    constant = numpy.broadcast_to(constant, src.shape)
    saturate = numpy.minimum(a_s, 1 - a_d)
    saturate[..., 3] = 1
    table = {1: numpy.zeros_like(src), 3: src, 4: 1 - src, 5: a_s, 6: 1 - a_s, 7: a_d, 8: 1 - a_d, 9: dst, 10: 1 - dst,
             11: saturate, 14: constant, 15: 1 - constant, 16: src, 17: 1 - src, 18: a_s, 19: 1 - a_s}
    return table.get(code, numpy.ones_like(src))


def _combine(op, a, b):
    return {2: a - b, 3: b - a, 4: numpy.minimum(a, b), 5: numpy.maximum(a, b)}.get(op, a + b)


DEPTH_TESTS = {1: lambda z, d: numpy.zeros_like(z, bool), 2: numpy.less, 3: numpy.equal, 4: numpy.less_equal,
               5: numpy.greater, 6: numpy.not_equal, 7: numpy.greater_equal, 8: lambda z, d: numpy.ones_like(z, bool)}


class Blend:
    def __init__(self, enabled=False, src=2, dst=1, op=1, src_a=2, dst_a=1, op_a=1, write_mask=0xF):
        self.enabled, self.src, self.dst, self.op = enabled, src, dst, op
        self.src_a, self.dst_a, self.op_a, self.write_mask = src_a, dst_a, op_a, write_mask


def output(fb: Framebuffer, y0, x0, inside, rgba, z=None, depth_func=0, depth_write=False, blend: Blend = None,
           constant=(1.0, 1.0, 1.0, 1.0), depth=None):
    """Writes the pixels inside (h, w) of rgba (h, w, 4) floats: depth test (depth_func 0: none,
    Direct3D's comparison numbers else), blending, write mask."""
    h, w = inside.shape
    depth = fb.depth if depth is None else depth
    if depth_func and z is not None:
        region = depth[y0:y0 + h, x0:x0 + w]
        inside = inside & DEPTH_TESTS.get(depth_func, numpy.less_equal)(z, region)
        if depth_write:
            region[inside] = z[inside]
    if not inside.any():
        return
    target = fb.color[y0:y0 + h, x0:x0 + w]
    src = numpy.clip(numpy.nan_to_num(rgba), 0, 1)
    if blend is not None and blend.enabled:
        dst = target.astype(numpy.float32) / 255
        const = numpy.array(constant, dtype=numpy.float32)
        color = _combine(blend.op, src * _factor(blend.src, src, dst, const), dst * _factor(blend.dst, src, dst, const))
        alpha = _combine(blend.op_a, src * _factor(blend.src_a, src, dst, const), dst * _factor(blend.dst_a, src, dst, const))
        src = numpy.clip(numpy.concatenate([color[..., :3], alpha[..., 3:4]], axis=-1), 0, 1)
    pixels = numpy.round(src * 255).astype(numpy.uint8)
    mask = blend.write_mask if blend is not None else 0xF
    for c in range(4):
        if mask >> c & 1:
            target[..., c][inside] = pixels[..., c][inside]


# ------------------------------------------------------------------ Direct3D 9
FVF_TEXCOORD_SIZES = {0: 2, 1: 3, 2: 4, 3: 1}
D3DPT = {1: "points", 2: "lines", 3: "line_strip", 4: "triangles", 5: "strip", 6: "fan"}


def _vertex_count(kind: int, primitives_count: int) -> int:
    return {1: primitives_count, 2: 2 * primitives_count, 3: primitives_count + 1, 4: 3 * primitives_count,
            5: primitives_count + 2, 6: primitives_count + 2}.get(kind, 0)


def fvf_elements(fvf: int):
    """The elements (usage, usage index, type, offset) of an FVF, and the vertex size. Types as D3DDECLTYPE:
    0..3 FLOAT1..4, 4 D3DCOLOR."""
    elements, offset = [], 0
    position = fvf & 0x400E
    if position == 0x004:
        elements.append((9, 0, 3, offset))
        offset += 16
    elif position == 0x4002:
        elements.append((0, 0, 3, offset))
        offset += 16
    elif position:
        elements.append((0, 0, 2, offset))
        offset += 12
        if position >= 0x006:
            offset += 4 * ((position - 0x004) // 2)
    if fvf & 0x010:
        elements.append((3, 0, 2, offset))
        offset += 12
    if fvf & 0x020:
        elements.append((4, 0, 0, offset))
        offset += 4
    if fvf & 0x040:
        elements.append((10, 0, 4, offset))
        offset += 4
    if fvf & 0x080:
        elements.append((10, 1, 4, offset))
        offset += 4
    for i in range((fvf >> 8) & 0xF):
        size = FVF_TEXCOORD_SIZES[(fvf >> (16 + 2 * i)) & 3]
        elements.append((5, i, size - 1, offset))
        offset += 4 * size
    return elements, offset


def _decode9(data: bytes, offset: int, kind: int):
    if kind <= 3:
        return list(struct.unpack_from(f"<{kind + 1}f", data, offset))
    if kind == 4:  # D3DCOLOR: A R G B in a DWORD
        b, g, r, a = data[offset:offset + 4]
        return [r / 255, g / 255, b / 255, a / 255]
    if kind == 5:
        return list(data[offset:offset + 4])
    if kind == 8:  # UBYTE4N
        return [x / 255 for x in data[offset:offset + 4]]
    if kind == 6:
        return [float(x) for x in struct.unpack_from("<2h", data, offset)]
    if kind == 7:
        return [float(x) for x in struct.unpack_from("<4h", data, offset)]
    if kind == 15:
        return [float(x) for x in numpy.frombuffer(data[offset:offset + 4], dtype=numpy.float16)]
    if kind == 16:
        return [float(x) for x in numpy.frombuffer(data[offset:offset + 8], dtype=numpy.float16)]
    return [0.0, 0.0, 0.0, 1.0]


class Surface9:
    def __init__(self, emu, image: Image, fmt: int, pool=0, usage=0, container=None):
        self.emu, self.image, self.fmt, self.pool, self.usage, self.container = emu, image, fmt, pool, usage, container

    def GetDesc(self, desc):
        self.emu.dp.write(desc, struct.pack("<8I", self.fmt, 1, self.usage, self.pool, 0, 0, self.image.width,
                                            self.image.height))

    def LockRect(self, locked, rect, flags):
        dp = self.emu.dp
        address = self.image.map(dp)
        if rect:
            left, top, _, _ = struct.unpack("<4i", bytes(dp.read(rect, 16)))
            address += top * self.image.pitch + left * (self.image.pitch // self.image.width)
        dp.write(locked, struct.pack("<i", self.image.pitch) + b"\0" * (dp.ptr_size() - 4))
        dp.write_ptr(locked + dp.ptr_size(), address)

    def UnlockRect(self):
        self.image.unmap(self.emu.dp)

    def GetDevice(self, out):
        return self.emu.device_out(out)

    def GetType(self):
        return 1  # D3DRTYPE_SURFACE

    def GetContainer(self, riid, out):
        if self.container is not None:
            return self.emu.query(self.container, riid, out)
        return self.emu.device_out(out) if read_guid(self.emu.dp, riid) == IIDS["IDirect3DDevice9"] else E_NOINTERFACE

    def GetDC(self, out):
        """A GDI memory DC on the pixels (gui_draw.py), written back at ReleaseDC."""
        gui = getattr(self.emu.dp, "gui", None)
        drawing = getattr(gui, "drawing", None)
        if drawing is None or not drawing.enabled:
            return E_NOTIMPL
        from PIL import Image as PilImage
        from .gui_draw import GdiObject
        hdc = drawing.new_dc(0)
        bitmap = GdiObject("bitmap", image=PilImage.fromarray(self.image.fb.color[..., :3].copy(), "RGB"))
        drawing.dcs[hdc].bitmap = bitmap
        self.dc = (hdc, bitmap)
        self.emu.dp.write_ptr(out, hdc)

    def ReleaseDC(self, hdc):
        dc = getattr(self, "dc", None)
        if dc is None:
            return D3DERR_INVALIDCALL
        if dc[1].image is not None:
            self.image.fb.color[..., :3] = numpy.array(dc[1].image.convert("RGB"))
        self.emu.dp.gui.drawing.dcs.pop(dc[0], None)
        self.dc = None


class Texture9:
    def __init__(self, emu, width, height, levels, usage, fmt, pool):
        self.emu, self.fmt, self.usage, self.pool = emu, fmt, usage, pool
        rgba, no_alpha = D3D9_RGBA.get(fmt) or D3D9_BGRA.get(fmt) or (False, False)
        self.levels = []
        count = levels or 1 + max(width, height).bit_length() - 1
        for level in range(count):
            image = Image(max(1, width >> level), max(1, height >> level), rgba, no_alpha)
            self.levels.append(Surface9(emu, image, fmt, pool, usage, self))
            if width >> level <= 1 and height >> level <= 1:
                break

    def GetLevelCount(self):
        return len(self.levels)

    def GetLevelDesc(self, level, desc):
        if level >= len(self.levels):
            return D3DERR_INVALIDCALL
        return self.levels[level].GetDesc(desc)

    def GetSurfaceLevel(self, level, out):
        if level >= len(self.levels):
            return D3DERR_INVALIDCALL
        self.emu.dp.write_ptr(out, self.emu.com.create(self.levels[level], "IDirect3DSurface9"))
        self.levels[level].refs += 1

    def LockRect(self, level, locked, rect, flags):
        if level >= len(self.levels):
            return D3DERR_INVALIDCALL
        return self.levels[level].LockRect(locked, rect, flags)

    def UnlockRect(self, level):
        if level < len(self.levels):
            self.levels[level].UnlockRect()

    def GetType(self):
        return 3  # D3DRTYPE_TEXTURE

    def GetDevice(self, out):
        return self.emu.device_out(out)

    def SetLOD(self, lod):
        return 0

    def GetLOD(self):
        return 0

    def AddDirtyRect(self, rect):
        return 0

    def GenerateMipSubLevels(self):
        return None


class Buffer9:
    """A vertex or index buffer: its bytes stay in the emulator's memory."""

    def __init__(self, emu, length, usage, fvf_or_format, pool, index=False):
        self.emu, self.length, self.usage, self.format, self.pool, self.index = emu, length, usage, fvf_or_format, pool, index
        self.address = emu.dp.allocate(max(16, length), page_align=True)

    def Lock(self, offset, size, out, flags):
        self.emu.dp.write_ptr(out, self.address + offset)

    def Unlock(self):
        return 0

    def GetDesc(self, desc):
        values = (self.format, 2 if self.index else 1, self.usage, self.pool, self.length) + (() if self.index else (self.format,))
        self.emu.dp.write(desc, struct.pack(f"<{len(values)}I", *values))

    def GetType(self):
        return 4 if self.index else 5

    def GetDevice(self, out):
        return self.emu.device_out(out)

    def data(self) -> bytes:
        return bytes(self.emu.dp.read(self.address, self.length))


class Plain9:
    """Vertex declarations, shaders, state blocks: kept, not run."""

    def __init__(self, emu, kind, data=None):
        self.emu, self.kind, self.data = emu, kind, data

    def GetDevice(self, out):
        return self.emu.device_out(out)

    def Capture(self):
        return 0

    def Apply(self):
        return 0


class SwapChain9:
    def __init__(self, emu, device):
        self.emu, self.device = emu, device

    def Present(self, src, dst, window, region, flags):
        return self.device.Present(src, dst, window, region)

    def GetBackBuffer(self, index, kind, out):
        return self.device.GetBackBuffer(0, index, kind, out)

    def GetPresentParameters(self, out):
        self.emu.dp.write(out, self.device.parameters_bytes)

    def GetDevice(self, out):
        return self.emu.device_out(out)

    def GetDisplayMode(self, mode):
        return self.device.GetDisplayMode(0, mode)


class Device9:
    def __init__(self, emu, d3d, window, flags, parameters):
        self.emu, self.d3d, self.focus, self.flags = emu, d3d, window, flags
        self.swapchain = SwapChain9(emu, self)
        self.read_parameters(parameters)
        self.render_states: Dict[int, int] = {7: 1 if self.auto_depth else 0, 8: 3, 9: 2, 14: 1, 15: 0, 19: 2, 20: 1,
                                              22: 3, 23: 4, 27: 0, 137: 1, 139: 0, 141: 1, 145: 1, 146: 0, 147: 0,
                                              148: 0, 171: 1, 174: 0, 207: 0, 208: 2, 209: 1}
        self.transforms = {2: numpy.identity(4), 3: numpy.identity(4), 256: numpy.identity(4)}
        self.fvf = 0
        self.declaration: Optional[Plain9] = None
        self.streams: Dict[int, tuple] = {}
        self.indices: Optional[Buffer9] = None
        self.textures: Dict[int, Optional[Texture9]] = {}
        self.texture_stages: Dict[tuple, int] = {}
        self.samplers: Dict[tuple, int] = {}
        self.material = [1.0] * 4 + [0.0] * 4 + [0.0] * 4 + [0.0] * 4 + [0.0]
        self.lights: Dict[int, list] = {}
        self.lights_on: Dict[int, bool] = {}
        self.vertex_shader = self.pixel_shader = None
        self.depth_surface = Surface9(emu, self.back.image, 75, 0, 2) if self.auto_depth else None

    def read_parameters(self, address):
        dp = self.emu.dp
        size = 64 if dp._x64 else 56
        data = bytearray(dp.read(address, size))
        width, height, fmt = struct.unpack_from("<3I", data, 0)
        window = dp.read_ptr(address + (32 if dp._x64 else 28))
        windowed, auto_depth = struct.unpack_from("<2I", data, 40 if dp._x64 else 32)
        self.window = window or self.focus
        if windowed and (not width or not height):
            canvas = self.emu.canvas(self.window)
            width, height = canvas.size if canvas is not None else (640, 480)
            struct.pack_into("<3I", data, 0, width, height, fmt or 22)
            dp.write(address, bytes(data[:12]))
        self.parameters_bytes = bytes(data)
        self.format = fmt or 22
        self.auto_depth = bool(auto_depth)
        rgba, no_alpha = D3D9_RGBA.get(self.format) or D3D9_BGRA.get(self.format) or (False, True)
        self.back = Surface9(self.emu, Image(width or 640, height or 480, rgba, no_alpha), self.format, 0, 1)
        self.target = self.back
        self.viewport = [0, 0, self.back.image.width, self.back.image.height, 0.0, 1.0]

    # -------------------------------------------------------------- device
    def TestCooperativeLevel(self):
        return 0

    def GetAvailableTextureMem(self):
        return 512 << 20

    def EvictManagedResources(self):
        return 0

    def GetDirect3D(self, out):
        self.emu.dp.write_ptr(out, self.emu.com.create(self.d3d, "IDirect3D9"))
        self.d3d.refs += 1

    def GetDeviceCaps(self, caps):
        return self.d3d.GetDeviceCaps(0, 1, caps)

    def GetDisplayMode(self, swapchain, mode):
        return self.d3d.GetAdapterDisplayMode(0, mode)

    def GetCreationParameters(self, out):
        dp = self.emu.dp
        dp.write(out, struct.pack("<2I", 0, 1))
        dp.write_ptr(out + 8, self.focus)
        dp.write(out + 8 + dp.ptr_size(), struct.pack("<I", self.flags))

    def SetCursorProperties(self, x, y, surface):
        return 0

    def SetCursorPosition(self, x, y, flags):
        return None

    def ShowCursor(self, show):
        return 1

    def GetSwapChain(self, index, out):
        if index:
            return D3DERR_INVALIDCALL
        self.emu.dp.write_ptr(out, self.emu.com.create(self.swapchain, "IDirect3DSwapChain9"))
        self.swapchain.refs += 1

    def GetNumberOfSwapChains(self):
        return 1

    def Reset(self, parameters):
        self.read_parameters(parameters)
        return 0

    def Present(self, src, dst, window, region):
        self.emu.present(window or self.window, self.back.image)
        return 0

    def GetBackBuffer(self, swapchain, index, kind, out):
        self.emu.dp.write_ptr(out, self.emu.com.create(self.back, "IDirect3DSurface9"))
        self.back.refs += 1

    def GetRasterStatus(self, swapchain, out):
        self.emu.dp.write(out, struct.pack("<2I", 1, 0))

    def SetDialogBoxMode(self, enable):
        return 0

    def SetGammaRamp(self, swapchain, flags, ramp):
        return None

    def GetGammaRamp(self, swapchain, ramp):
        self.emu.dp.write(ramp, b"".join(struct.pack("<256H", *[i * 257 for i in range(256)]) for _ in range(3)))

    # -------------------------------------------------------------- resources
    def CreateTexture(self, width, height, levels, usage, fmt, pool, out, shared):
        texture = Texture9(self.emu, width, height, levels, usage, fmt, pool)
        self.emu.dp.write_ptr(out, self.emu.com.create(texture, "IDirect3DTexture9"))

    def CreateVertexBuffer(self, length, usage, fvf, pool, out, shared):
        self.emu.dp.write_ptr(out, self.emu.com.create(Buffer9(self.emu, length, usage, fvf, pool), "IDirect3DVertexBuffer9"))

    def CreateIndexBuffer(self, length, usage, fmt, pool, out, shared):
        self.emu.dp.write_ptr(out, self.emu.com.create(Buffer9(self.emu, length, usage, fmt, pool, True), "IDirect3DIndexBuffer9"))

    def _surface(self, width, height, fmt, pool, usage, out):
        rgba, no_alpha = D3D9_RGBA.get(fmt) or D3D9_BGRA.get(fmt) or (False, False)
        surface = Surface9(self.emu, Image(width, height, rgba, no_alpha), fmt, pool, usage)
        self.emu.dp.write_ptr(out, self.emu.com.create(surface, "IDirect3DSurface9"))

    def CreateRenderTarget(self, width, height, fmt, multisample, quality, lockable, out, shared):
        self._surface(width, height, fmt, 0, 1, out)

    def CreateDepthStencilSurface(self, width, height, fmt, multisample, quality, discard, out, shared):
        self._surface(width, height, fmt, 0, 2, out)

    def CreateOffscreenPlainSurface(self, width, height, fmt, pool, out, shared):
        self._surface(width, height, fmt, pool, 0, out)

    def _surface_of(self, address) -> Optional[Surface9]:
        impl = self.emu.com.impl(address)
        return impl if isinstance(impl, Surface9) else None

    def UpdateSurface(self, src, rect, dst, point):
        source, target = self._surface_of(src), self._surface_of(dst)
        if source is None or target is None:
            return D3DERR_INVALIDCALL
        dp = self.emu.dp
        area = struct.unpack("<4i", bytes(dp.read(rect, 16))) if rect else None
        at = struct.unpack("<2i", bytes(dp.read(point, 8))) if point else (area[0], area[1]) if area else (0, 0)
        target.image.copy_from(source.image, area, at)

    def UpdateTexture(self, src, dst):
        source, target = self.emu.com.impl(src), self.emu.com.impl(dst)
        if not isinstance(source, Texture9) or not isinstance(target, Texture9):
            return D3DERR_INVALIDCALL
        for a, b in zip(source.levels, target.levels):
            b.image.copy_from(a.image)

    def GetRenderTargetData(self, src, dst):
        source, target = self._surface_of(src), self._surface_of(dst)
        if source is None or target is None:
            return D3DERR_INVALIDCALL
        target.image.copy_from(source.image)

    def GetFrontBufferData(self, swapchain, dst):
        target = self._surface_of(dst)
        if target is None:
            return D3DERR_INVALIDCALL
        target.image.copy_from(self.back.image)

    def StretchRect(self, src, src_rect, dst, dst_rect, filter_type):
        source, target = self._surface_of(src), self._surface_of(dst)
        if source is None or target is None:
            return D3DERR_INVALIDCALL
        dp = self.emu.dp
        a = struct.unpack("<4i", bytes(dp.read(src_rect, 16))) if src_rect else (0, 0, source.image.width, source.image.height)
        b = struct.unpack("<4i", bytes(dp.read(dst_rect, 16))) if dst_rect else (0, 0, target.image.width, target.image.height)
        from PIL import Image as PilImage
        part = PilImage.fromarray(source.image.fb.color[a[1]:a[3], a[0]:a[2]].copy(), "RGBA")
        width, height = b[2] - b[0], b[3] - b[1]
        if width <= 0 or height <= 0 or part.width == 0 or part.height == 0:
            return D3DERR_INVALIDCALL
        part = part.resize((width, height), PilImage.BILINEAR if filter_type >= 2 else PilImage.NEAREST)
        target.image.fb.color[b[1]:b[3], b[0]:b[2]] = numpy.array(part)[:target.image.height - b[1], :target.image.width - b[0]]

    def ColorFill(self, surface, rect, color):
        target = self._surface_of(surface)
        if target is None:
            return D3DERR_INVALIDCALL
        area = struct.unpack("<4i", bytes(self.emu.dp.read(rect, 16))) if rect else None
        target.image.fb.clear(_argb(color), None, area)

    def SetRenderTarget(self, index, surface):
        target = self._surface_of(surface)
        if index == 0:
            if target is None:
                return D3DERR_INVALIDCALL
            self.target = target
            self.viewport = [0, 0, target.image.width, target.image.height, 0.0, 1.0]

    def GetRenderTarget(self, index, out):
        if index:
            self.emu.dp.write_ptr(out, 0)
            return 0x8876086A
        self.emu.dp.write_ptr(out, self.emu.com.create(self.target, "IDirect3DSurface9"))
        self.target.refs += 1

    def SetDepthStencilSurface(self, surface):
        self.depth_surface = self._surface_of(surface)

    def GetDepthStencilSurface(self, out):
        if self.depth_surface is None:
            self.emu.dp.write_ptr(out, 0)
            return 0x8876086A  # D3DERR_NOTFOUND
        self.emu.dp.write_ptr(out, self.emu.com.create(self.depth_surface, "IDirect3DSurface9"))
        self.depth_surface.refs += 1

    # -------------------------------------------------------------- state
    def BeginScene(self):
        return 0

    def EndScene(self):
        return 0

    def Clear(self, count, rects, flags, color, z, stencil):
        fb = self.target.image.fb
        dp = self.emu.dp
        x, y, w, h = self.viewport[:4]
        areas = [struct.unpack_from("<4i", bytes(dp.read(rects + 16 * i, 16))) for i in range(count)] if rects and count \
            else [(x, y, x + w, y + h)]
        for area in areas:
            if flags & 1:
                fb.clear(_argb(color), None, area)
            if flags & 2:
                self._depth_buffer().clear(None, z, area)

    def _depth_buffer(self) -> Framebuffer:
        """The depth values: those of the render target's framebuffer (a depth surface of its size)."""
        return self.target.image.fb

    def SetTransform(self, state, matrix):
        self.transforms[state] = numpy.array(struct.unpack("<16f", bytes(self.emu.dp.read(matrix, 64)))).reshape(4, 4)

    def GetTransform(self, state, matrix):
        self.emu.dp.write(matrix, struct.pack("<16f", *self.transforms.get(state, numpy.identity(4)).flatten()))

    def MultiplyTransform(self, state, matrix):
        m = numpy.array(struct.unpack("<16f", bytes(self.emu.dp.read(matrix, 64)))).reshape(4, 4)
        self.transforms[state] = m @ self.transforms.get(state, numpy.identity(4))

    def SetViewport(self, viewport):
        x, y, w, h, mn, mx = struct.unpack("<4I2f", bytes(self.emu.dp.read(viewport, 24)))
        self.viewport = [x, y, w, h, mn, mx]

    def GetViewport(self, viewport):
        self.emu.dp.write(viewport, struct.pack("<4I2f", *self.viewport))

    def SetMaterial(self, material):
        self.material = list(struct.unpack("<17f", bytes(self.emu.dp.read(material, 68))))

    def GetMaterial(self, material):
        self.emu.dp.write(material, struct.pack("<17f", *self.material))

    def SetLight(self, index, light):
        data = bytes(self.emu.dp.read(light, 104))
        self.lights[index] = [struct.unpack_from("<I", data, 0)[0]] + list(struct.unpack_from("<25f", data, 4))

    def GetLight(self, index, light):
        if index not in self.lights:
            return D3DERR_INVALIDCALL
        self.emu.dp.write(light, struct.pack("<I25f", self.lights[index][0], *self.lights[index][1:]))

    def LightEnable(self, index, enable):
        if index not in self.lights:
            self.lights[index] = [3, 1.0, 1.0, 1.0, 0.0] + [0.0] * 8 + [0.0, 0.0, 1.0] + [0.0] * 6
        self.lights_on[index] = bool(enable)

    def GetLightEnable(self, index, out):
        self.emu.dp.write(out, struct.pack("<I", int(self.lights_on.get(index, False))))

    def SetClipPlane(self, index, plane):
        return 0

    def GetClipPlane(self, index, plane):
        self.emu.dp.write(plane, b"\0" * 16)

    def SetRenderState(self, state, value):
        self.render_states[state] = value

    def GetRenderState(self, state, out):
        self.emu.dp.write(out, struct.pack("<I", self.render_states.get(state, 0) & 0xFFFFFFFF))

    def CreateStateBlock(self, kind, out):
        self.emu.dp.write_ptr(out, self.emu.com.create(Plain9(self.emu, "stateblock"), "IDirect3DStateBlock9"))

    def BeginStateBlock(self):
        return 0

    def EndStateBlock(self, out):
        return self.CreateStateBlock(0, out)

    def SetClipStatus(self, status):
        return 0

    def GetClipStatus(self, status):
        self.emu.dp.write(status, b"\0" * 8)

    def GetTexture(self, stage, out):
        texture = self.textures.get(stage)
        self.emu.dp.write_ptr(out, self.emu.com.create(texture, "IDirect3DTexture9") if texture else 0)
        if texture:
            texture.refs += 1

    def SetTexture(self, stage, texture):
        impl = self.emu.com.impl(texture)
        self.textures[stage] = impl if isinstance(impl, Texture9) else None

    def GetTextureStageState(self, stage, kind, out):
        self.emu.dp.write(out, struct.pack("<I", self.texture_stages.get((stage, kind), 0) & 0xFFFFFFFF))

    def SetTextureStageState(self, stage, kind, value):
        self.texture_stages[(stage, kind)] = value

    def GetSamplerState(self, sampler, kind, out):
        self.emu.dp.write(out, struct.pack("<I", self.samplers.get((sampler, kind), 1 if kind in (1, 2, 3) else 0) & 0xFFFFFFFF))

    def SetSamplerState(self, sampler, kind, value):
        self.samplers[(sampler, kind)] = value

    def ValidateDevice(self, passes):
        self.emu.dp.write(passes, struct.pack("<I", 1))

    def SetScissorRect(self, rect):
        self.scissor = struct.unpack("<4i", bytes(self.emu.dp.read(rect, 16)))

    def GetScissorRect(self, rect):
        self.emu.dp.write(rect, struct.pack("<4i", *getattr(self, "scissor", (0, 0, self.back.image.width, self.back.image.height))))

    def SetSoftwareVertexProcessing(self, software):
        return 0

    def GetSoftwareVertexProcessing(self):
        return 1

    def SetNPatchMode(self, segments):
        return 0

    def CreateVertexDeclaration(self, elements, out):
        dp = self.emu.dp
        items = []
        for i in range(64):
            stream, offset, kind, method, usage, usage_index = struct.unpack("<2H4B", bytes(dp.read(elements + 8 * i, 8)))
            if stream == 0xFF:
                break
            items.append((stream, offset, kind, usage, usage_index))
        dp.write_ptr(out, self.emu.com.create(Plain9(self.emu, "declaration", items), "IDirect3DVertexDeclaration9"))

    def SetVertexDeclaration(self, declaration):
        impl = self.emu.com.impl(declaration)
        self.declaration = impl if isinstance(impl, Plain9) else None
        if self.declaration is not None:
            self.fvf = 0

    def GetVertexDeclaration(self, out):
        self.emu.dp.write_ptr(out, self.declaration.com_address if self.declaration else 0)

    def SetFVF(self, fvf):
        self.fvf = fvf
        if fvf:
            self.declaration = None

    def GetFVF(self, out):
        self.emu.dp.write(out, struct.pack("<I", self.fvf))

    def CreateVertexShader(self, function, out):
        self.emu.dp.write_ptr(out, self.emu.com.create(Plain9(self.emu, "vertex shader"), "IDirect3DVertexShader9"))

    def SetVertexShader(self, shader):
        self.vertex_shader = self.emu.com.impl(shader)

    def GetVertexShader(self, out):
        self.emu.dp.write_ptr(out, self.vertex_shader.com_address if self.vertex_shader else 0)

    def CreatePixelShader(self, function, out):
        self.emu.dp.write_ptr(out, self.emu.com.create(Plain9(self.emu, "pixel shader"), "IDirect3DPixelShader9"))

    def SetPixelShader(self, shader):
        self.pixel_shader = self.emu.com.impl(shader)

    def GetPixelShader(self, out):
        self.emu.dp.write_ptr(out, self.pixel_shader.com_address if self.pixel_shader else 0)

    def SetVertexShaderConstantF(self, start, data, count):
        return 0

    SetVertexShaderConstantI = SetVertexShaderConstantB = SetVertexShaderConstantF
    SetPixelShaderConstantF = SetPixelShaderConstantI = SetPixelShaderConstantB = SetVertexShaderConstantF

    def SetStreamSource(self, stream, buffer, offset, stride):
        impl = self.emu.com.impl(buffer)
        self.streams[stream] = (impl, offset, stride) if impl is not None else None

    def GetStreamSource(self, stream, out, offset, stride):
        entry = self.streams.get(stream)
        dp = self.emu.dp
        dp.write_ptr(out, entry[0].com_address if entry else 0)
        dp.write(offset, struct.pack("<I", entry[1] if entry else 0))
        dp.write(stride, struct.pack("<I", entry[2] if entry else 0))

    def SetStreamSourceFreq(self, stream, setting):
        return 0

    def SetIndices(self, buffer):
        impl = self.emu.com.impl(buffer)
        self.indices = impl if isinstance(impl, Buffer9) else None

    def GetIndices(self, out):
        self.emu.dp.write_ptr(out, self.indices.com_address if self.indices else 0)

    def CreateQuery(self, kind, out):
        return 0 if not out else D3DERR_NOTAVAILABLE

    # -------------------------------------------------------------- drawing
    def DrawPrimitiveUP(self, kind, count, data, stride):
        n = _vertex_count(kind, count)
        vertices = bytes(self.emu.dp.read(data, n * stride)) if n else b""
        self._draw(kind, {0: (vertices, 0, stride)}, list(range(n)))

    def DrawIndexedPrimitiveUP(self, kind, min_index, vertex_count, count, indices, index_format, data, stride):
        n = _vertex_count(kind, count)
        size = 4 if index_format == 102 else 2
        index_list = list(struct.unpack(f"<{n}{'I' if size == 4 else 'H'}", bytes(self.emu.dp.read(indices, n * size))))
        top = max(index_list) + 1 if index_list else 0
        vertices = bytes(self.emu.dp.read(data, top * stride))
        self._draw(kind, {0: (vertices, 0, stride)}, index_list)

    def _streams(self):
        out = {}
        for stream, entry in self.streams.items():
            if entry:
                buffer, offset, stride = entry
                out[stream] = (buffer.data(), offset, stride)
        return out

    def DrawPrimitive(self, kind, start, count):
        n = _vertex_count(kind, count)
        self._draw(kind, self._streams(), list(range(start, start + n)))

    def DrawIndexedPrimitive(self, kind, base, min_index, vertex_count, start, count):
        if self.indices is None:
            return D3DERR_INVALIDCALL
        n = _vertex_count(kind, count)
        size = 4 if self.indices.format == 102 else 2
        data = self.indices.data()
        index_list = list(struct.unpack_from(f"<{n}{'I' if size == 4 else 'H'}", data, start * size))
        base = base - (1 << 32) if base & 0x80000000 else base
        self._draw(kind, self._streams(), [i + base for i in index_list])

    def _elements(self):
        if self.declaration is not None:
            return [(stream, offset, kind, usage, index) for stream, offset, kind, usage, index in self.declaration.data]
        elements, _ = fvf_elements(self.fvf)
        return [(0, offset, kind, usage, index) for usage, index, kind, offset in elements]

    def _draw(self, kind: int, streams, index_list: List[int]):
        dp = self.emu.dp
        if self.vertex_shader is not None or self.pixel_shader is not None:
            _missing(dp, "Direct3D 9 shaders")
            return 0
        elements = self._elements()
        rs = self.render_states
        fb = self.target.image.fb
        vx, vy, vw, vh, vmin, vmax = self.viewport
        world, view, projection = self.transforms.get(256), self.transforms.get(2), self.transforms.get(3)
        transform = world @ view @ projection
        lighting = rs.get(137, 1) and not any(e[3] == 9 for e in elements)
        texture = self.textures.get(0)
        texels = texture.levels[0].image.texels() if texture is not None and self.texture_stages.get((0, 1), 4) != 1 else None
        linear = self.samplers.get((0, 5), 1) >= 2
        address = {1: "wrap", 2: "mirror", 3: "clamp", 4: "border"}.get(self.samplers.get((0, 1), 1), "wrap")
        cache = {}
        screen = []
        for index in index_list:
            if index in cache:
                screen.append(cache[index])
                continue
            values = {}
            for stream, offset, element_kind, usage, usage_index in elements:
                source = streams.get(stream)
                if source is None:
                    continue
                data, base, stride = source
                at = base + index * stride + offset
                if at + 4 > len(data):
                    continue
                values[(usage, usage_index)] = _decode9(data, at, element_kind)
            color = values.get((10, 0), [1.0, 1.0, 1.0, 1.0])
            if (9, 0) in values:  # POSITIONT: already on the screen
                x, y, z, rhw = (values[(9, 0)] + [0.0, 0.0, 0.0, 1.0])[:4]
                point = (x, y, z, rhw or 1.0)
            else:
                position = (values.get((0, 0), [0.0, 0.0, 0.0]) + [0.0, 0.0, 1.0])[:3] + [1.0]
                clip = numpy.array(position) @ transform
                if clip[3] <= 1e-9:
                    point = None
                else:
                    ndc = clip[:3] / clip[3]
                    point = (vx + (ndc[0] + 1) * vw / 2, vy + (1 - ndc[1]) * vh / 2, vmin + ndc[2] * (vmax - vmin), 1 / clip[3])
                if lighting:
                    color = self._light(position, values.get((3, 0)), color, world @ view)
            uv = (values.get((5, 0)) or [0.0, 0.0])[:2]
            item = None if point is None else (point, list(color), uv)
            cache[index] = item
            screen.append(item)
        cull = {1: 0, 2: 2, 3: 1}.get(rs.get(22, 3), 1)
        flat = rs.get(9, 2) == 1
        depth_func = rs.get(23, 4) if rs.get(7, 0) else 0
        blend = Blend(bool(rs.get(27, 0)), rs.get(19, 2), rs.get(20, 1), rs.get(171, 1), rs.get(19, 2), rs.get(20, 1),
                      rs.get(171, 1), rs.get(168, 0xF) & 0xF)
        for indices in primitives(D3DPT.get(kind, "points"), len(screen)):
            points = [screen[i] for i in indices]
            if any(p is None for p in points):
                continue
            if len(points) == 3:
                self._triangle(fb, points, cull, flat, depth_func, bool(rs.get(14, 1)), blend, texels, linear, address)
            elif len(points) == 2:
                fb.lines([(p[0][0], p[0][1]) for p in points], points[0][1])
            else:
                fb.points([(points[0][0][0], points[0][0][1])], points[0][1])
        return 0

    @staticmethod
    def _triangle(fb, points, cull, flat, depth_func, depth_write, blend, texels, linear, address):
        covered = fb.cover(points[0][0][:3], points[1][0][:3], points[2][0][:3], cull)
        if covered is None:
            return
        y0, x0, inside, w0, w1, w2, z = covered
        inv_w = [p[0][3] for p in points]
        p0, p1, p2 = w0 * inv_w[0], w1 * inv_w[1], w2 * inv_w[2]
        total = p0 + p1 + p2
        total[total == 0] = 1
        b0, b1, b2 = p0 / total, p1 / total, p2 / total
        colors = numpy.array([p[1] for p in points])
        if flat:
            rgba = numpy.broadcast_to(colors[0], w0.shape + (4,)).copy()
        else:
            rgba = b0[..., None] * colors[0] + b1[..., None] * colors[1] + b2[..., None] * colors[2]
        if texels is not None:
            uv = numpy.array([p[2] for p in points])
            u = b0 * uv[0][0] + b1 * uv[1][0] + b2 * uv[2][0]
            v = b0 * uv[0][1] + b1 * uv[1][1] + b2 * uv[2][1]
            rgba = rgba * sample(texels, u, v, linear, address)
        output(fb, y0, x0, inside, rgba, z, depth_func, depth_write, blend)

    def _light(self, position, normal, vertex_color, world_view):
        """The fixed-function lighting (directional, point and spot as point lights; no specular)."""
        rs = self.render_states
        material = self.material
        sources = {0: None, 1: vertex_color}
        diffuse = sources.get(rs.get(145, 1)) if rs.get(141, 1) else None
        diffuse = diffuse if diffuse is not None else material[0:4]
        ambient = sources.get(rs.get(147, 0)) or material[4:8]
        emissive = sources.get(rs.get(148, 0)) or material[12:16]
        ambient_light = _argb(rs.get(139, 0))
        color = numpy.array(emissive[:3]) + numpy.array(ambient[:3]) * numpy.array(ambient_light[:3])
        if normal is not None:
            n = numpy.array(normal[:3] + [0.0]) @ world_view
            length = numpy.linalg.norm(n[:3])
            n = n[:3] / length if length else n[:3]
            p = (numpy.array(position) @ world_view)[:3]
            for index, on in self.lights_on.items():
                if not on:
                    continue
                light = self.lights[index]
                kind = light[0]
                light_diffuse, light_ambient = numpy.array(light[1:4]), numpy.array(light[9:12])
                if kind == 3:
                    direction = -(numpy.array(light[16:19] + [0.0]) @ self.transforms[2])[:3]
                else:
                    direction = (numpy.array(light[13:16] + [1.0]) @ self.transforms[2])[:3] - p
                length = numpy.linalg.norm(direction)
                direction = direction / length if length else direction
                color += numpy.array(ambient[:3]) * light_ambient
                color += numpy.array(diffuse[:3]) * light_diffuse * max(0.0, float(n @ direction))
        return list(numpy.clip(color, 0, 1)) + [diffuse[3] if len(diffuse) > 3 else 1.0]


def _argb(value: int):
    value &= 0xFFFFFFFF
    return [(value >> 16 & 0xFF) / 255, (value >> 8 & 0xFF) / 255, (value & 0xFF) / 255, (value >> 24) / 255]


class Direct3D9:
    def __init__(self, emu):
        self.emu = emu

    def RegisterSoftwareDevice(self, function):
        return 0

    def GetAdapterCount(self):
        return 1

    def GetAdapterIdentifier(self, adapter, flags, out):
        if adapter:
            return D3DERR_INVALIDCALL
        dp = self.emu.dp
        dp.write(out, b"\0" * 1100)
        dp.write(out, b"fathom.dll\0")
        dp.write(out + 512, b"Fathom software rasterizer\0")
        dp.write(out + 1024, b"\\\\.\\DISPLAY1\0")

    def GetAdapterModeCount(self, adapter, fmt):
        return 1

    def EnumAdapterModes(self, adapter, fmt, mode, out):
        return self.GetAdapterDisplayMode(adapter, out)

    def GetAdapterDisplayMode(self, adapter, out):
        width, height = self.emu.screen_size()
        self.emu.dp.write(out, struct.pack("<4I", width, height, 60, 22))

    def CheckDeviceType(self, adapter, kind, display, back, windowed):
        return 0

    def CheckDeviceFormat(self, adapter, kind, display, usage, resource, fmt):
        return 0

    def CheckDeviceMultiSampleType(self, adapter, kind, fmt, windowed, multisample, quality):
        if quality:
            self.emu.dp.write(quality, struct.pack("<I", 1))
        return 0 if multisample == 0 else D3DERR_NOTAVAILABLE

    def CheckDepthStencilMatch(self, adapter, kind, adapter_format, target, depth):
        return 0

    def CheckDeviceFormatConversion(self, adapter, kind, source, target):
        return 0

    def GetDeviceCaps(self, adapter, kind, caps):
        """D3DCAPS9: the fixed-function pipeline, no shaders (VertexShaderVersion / PixelShaderVersion 0)."""
        data = bytearray(304)
        struct.pack_into("<2I", data, 0, kind or 1, adapter)
        struct.pack_into("<I", data, 28, 0x10000)            # DevCaps: D3DDEVCAPS_HWTRANSFORMANDLIGHT
        struct.pack_into("<2I", data, 88, 4096, 4096)       # MaxTextureWidth / Height
        struct.pack_into("<f", data, 112, 1e10)             # MaxVertexW
        struct.pack_into("<2I", data, 148, 8, 8)            # MaxTextureBlendStages, MaxSimultaneousTextures
        struct.pack_into("<I", data, 160, 8)                 # MaxActiveLights
        struct.pack_into("<f", data, 176, 1.0)              # MaxPointSize
        struct.pack_into("<4I", data, 180, 0xFFFFF, 0xFFFFF, 16, 255)  # MaxPrimitiveCount, MaxVertexIndex, streams, stride
        self.emu.dp.write(caps, bytes(data))

    def GetAdapterMonitor(self, adapter):
        return 0x10001

    def CreateDevice(self, adapter, kind, window, flags, parameters, out):
        device = Device9(self.emu, self, window, flags, parameters)
        self.emu.devices9.append(device)
        self.emu.device9 = device
        self.emu.dp.write_ptr(out, self.emu.com.create(device, "IDirect3DDevice9"))
        self.emu.events.append(dict(kind="direct3d9_device", window=hex(device.window), width=device.back.image.width,
                                    height=device.back.image.height))


# ------------------------------------------------------------------ Direct3D 11 and DXGI
class Resource11:
    """A buffer (bytes in the emulator's memory) or a 2D texture (an Image per subresource)."""

    def __init__(self, emu, kind: str, desc: bytes):
        self.emu, self.kind, self.desc = emu, kind, desc
        self.images: List[Image] = []
        self.address = 0
        self.size = 0
        if kind == "buffer":
            self.size, self.usage, self.bind, self.cpu, self.misc, self.stride = struct.unpack("<6I", desc[:24])
            self.address = emu.dp.allocate(max(16, self.size), page_align=True)
        else:
            (self.width, self.height, self.mips, self.array, self.format, _, _, self.usage, self.bind, self.cpu,
             self.misc) = struct.unpack("<11I", desc[:44])
            rgba, no_alpha = DXGI_RGBA.get(self.format) or DXGI_BGRA.get(self.format) or (False, False)
            raw = RAW_BPP.get(self.format, 0) if self.format not in DXGI_RGBA and self.format not in DXGI_BGRA \
                and self.format not in DXGI_DEPTH else 0
            self.mips = self.mips or 1 + max(self.width, self.height).bit_length() - 1
            for _ in range(max(1, self.array)):
                for level in range(self.mips):
                    self.images.append(Image(max(1, self.width >> level), max(1, self.height >> level), rgba, no_alpha,
                                             self.format, raw))

    def GetDesc(self, out):
        self.emu.dp.write(out, self.desc)

    def GetType(self, out):
        self.emu.dp.write(out, struct.pack("<I", 1 if self.kind == "buffer" else 3))

    def GetDevice(self, out):
        return self.emu.device11_out(out)

    def SetEvictionPriority(self, priority):
        return None

    def GetEvictionPriority(self):
        return 0

    def SetPrivateData(self, guid, size, data):
        return 0

    def SetPrivateDataInterface(self, guid, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND

    def initial(self, data_address):
        if not data_address:
            return
        dp = self.emu.dp
        ptr = dp.ptr_size()
        if self.kind == "buffer":
            dp.write(self.address, bytes(dp.read(dp.read_ptr(data_address), self.size)))
            return
        stride = ptr + 8 if ptr == 8 else 12
        for index, image in enumerate(self.images):
            entry = data_address + stride * index
            source = dp.read_ptr(entry)
            pitch = struct.unpack("<I", bytes(dp.read(entry + ptr, 4)))[0]
            if source:
                image.from_bytes(bytes(dp.read(source, pitch * image.height)), pitch)

    def data(self) -> bytes:
        return bytes(self.emu.dp.read(self.address, self.size)) if self.kind == "buffer" else b""

    # IDXGISurface (a back buffer asked as a surface)
    def Map(self, locked, flags):
        dp = self.emu.dp
        image = self.images[0]
        dp.write(locked, struct.pack("<I", image.pitch) + b"\0" * (dp.ptr_size() - 4))
        dp.write_ptr(locked + dp.ptr_size(), image.map(dp))

    def Unmap(self):
        self.images[0].unmap(self.emu.dp)


class View11:
    def __init__(self, emu, resource: Resource11, kind: str, desc: Optional[bytes]):
        self.emu, self.resource, self.kind, self.desc_bytes = emu, resource, kind, desc
        self.subresource = 0  # mip 0 of the first slice (views of other subresources: approximated)

    @property
    def image(self) -> Optional[Image]:
        images = self.resource.images
        return images[min(self.subresource, len(images) - 1)] if images else None

    def GetResource(self, out):
        self.emu.dp.write_ptr(out, self.emu.com.create(self.resource, self.resource.interface))
        self.resource.refs += 1

    def GetDesc(self, out):
        if self.desc_bytes:
            self.emu.dp.write(out, self.desc_bytes)
        else:
            dimension = 1 if self.resource.kind == "buffer" else {"rtv": 4, "dsv": 3, "srv": 4}[self.kind]
            self.emu.dp.write(out, struct.pack("<2I", getattr(self.resource, "format", 0), dimension) + b"\0" * 16)

    def GetDevice(self, out):
        return self.emu.device11_out(out)


class State11:
    """Shaders, the input layout, samplers and the fixed states: kept, used at draw time."""

    def __init__(self, emu, kind, data=None):
        self.emu, self.kind, self.data = emu, kind, data

    def GetDesc(self, out):
        if isinstance(self.data, bytes):
            self.emu.dp.write(out, self.data)

    def GetDevice(self, out):
        return self.emu.device11_out(out)


class Context11:
    def __init__(self, emu, device):
        self.emu, self.device = emu, device
        self.ClearState()

    def ClearState(self):
        self.targets: List[Optional[View11]] = []
        self.depth: Optional[View11] = None
        self.viewports = []
        self.scissors = []
        self.topology = 0
        self.layout: Optional[State11] = None
        self.vertex_buffers: Dict[int, tuple] = {}
        self.index_buffer = None
        self.shaders: Dict[str, Optional[State11]] = {}
        self.constant_buffers: Dict[tuple, Resource11] = {}
        self.resources: Dict[tuple, View11] = {}
        self.sampler_states: Dict[tuple, State11] = {}
        self.rasterizer: Optional[State11] = None
        self.blend: Optional[State11] = None
        self.blend_factor = (1.0, 1.0, 1.0, 1.0)
        self.depth_state: Optional[State11] = None

    def GetDevice(self, out):
        return self.emu.device11_out(out)

    def GetType(self):
        return 0

    def GetContextFlags(self):
        return 0

    def Flush(self):
        return None

    def _list(self, count, array):
        dp = self.emu.dp
        return [dp.read_ptr(array + dp.ptr_size() * i) if array else 0 for i in range(count)]

    # shaders and their resources, for each stage
    def _set_shader(self, stage, shader):
        self.shaders[stage] = self.emu.com.impl(shader) if shader else None

    def _set_cbs(self, stage, start, count, array):
        for i, address in enumerate(self._list(count, array)):
            self.constant_buffers[(stage, start + i)] = self.emu.com.impl(address) if address else None

    def _set_srvs(self, stage, start, count, array):
        for i, address in enumerate(self._list(count, array)):
            self.resources[(stage, start + i)] = self.emu.com.impl(address) if address else None

    def _set_samplers(self, stage, start, count, array):
        for i, address in enumerate(self._list(count, array)):
            self.sampler_states[(stage, start + i)] = self.emu.com.impl(address) if address else None

    def _get(self, table, stage, start, count, array):
        dp = self.emu.dp
        for i in range(count):
            item = table.get((stage, start + i)) if isinstance(table, dict) else None
            dp.write_ptr(array + dp.ptr_size() * i, item.com_address if item is not None else 0)
            if item is not None:
                item.refs += 1

    def VSSetShader(self, shader, instances, count):
        self._set_shader("vs", shader)

    def PSSetShader(self, shader, instances, count):
        self._set_shader("ps", shader)

    def GSSetShader(self, shader, instances, count):
        self._set_shader("gs", shader)

    def HSSetShader(self, shader, instances, count):
        self._set_shader("hs", shader)

    def DSSetShader(self, shader, instances, count):
        self._set_shader("ds", shader)

    def CSSetShader(self, shader, instances, count):
        self._set_shader("cs", shader)

    def Dispatch(self, x, y, z):
        _missing(self.emu.dp, "Direct3D 11 compute shaders")

    def DispatchIndirect(self, buffer, offset):
        _missing(self.emu.dp, "Direct3D 11 compute shaders")

    def VSGetShader(self, out, instances, count):
        shader = self.shaders.get("vs")
        self.emu.dp.write_ptr(out, shader.com_address if shader else 0)

    def PSGetShader(self, out, instances, count):
        shader = self.shaders.get("ps")
        self.emu.dp.write_ptr(out, shader.com_address if shader else 0)

    def IASetInputLayout(self, layout):
        self.layout = self.emu.com.impl(layout) if layout else None

    def IAGetInputLayout(self, out):
        self.emu.dp.write_ptr(out, self.layout.com_address if self.layout else 0)

    def IASetVertexBuffers(self, start, count, buffers, strides, offsets):
        dp = self.emu.dp
        for i, address in enumerate(self._list(count, buffers)):
            stride = struct.unpack("<I", bytes(dp.read(strides + 4 * i, 4)))[0] if strides else 0
            offset = struct.unpack("<I", bytes(dp.read(offsets + 4 * i, 4)))[0] if offsets else 0
            self.vertex_buffers[start + i] = (self.emu.com.impl(address), stride, offset) if address else None

    def IASetIndexBuffer(self, buffer, fmt, offset):
        self.index_buffer = (self.emu.com.impl(buffer), fmt, offset) if buffer else None

    def IASetPrimitiveTopology(self, topology):
        self.topology = topology

    def IAGetPrimitiveTopology(self, out):
        self.emu.dp.write(out, struct.pack("<I", self.topology))

    def OMSetRenderTargets(self, count, views, depth):
        self.targets = [self.emu.com.impl(a) if a else None for a in self._list(count, views)]
        self.depth = self.emu.com.impl(depth) if depth else None

    def OMSetRenderTargetsAndUnorderedAccessViews(self, count, views, depth, uav_start, uav_count, uavs, initial):
        if count != 0xFFFFFFFF:
            self.OMSetRenderTargets(count, views, depth)

    def OMGetRenderTargets(self, count, views, depth):
        dp = self.emu.dp
        if views:
            for i in range(count):
                view = self.targets[i] if i < len(self.targets) else None
                dp.write_ptr(views + dp.ptr_size() * i, view.com_address if view else 0)
                if view:
                    view.refs += 1
        if depth:
            dp.write_ptr(depth, self.depth.com_address if self.depth else 0)
            if self.depth:
                self.depth.refs += 1

    def OMSetBlendState(self, state, factor, mask):
        self.blend = self.emu.com.impl(state) if state else None
        if factor:
            self.blend_factor = struct.unpack("<4f", bytes(self.emu.dp.read(factor, 16)))

    def OMSetDepthStencilState(self, state, reference):
        self.depth_state = self.emu.com.impl(state) if state else None

    def RSSetState(self, state):
        self.rasterizer = self.emu.com.impl(state) if state else None

    def RSSetViewports(self, count, viewports):
        self.viewports = [struct.unpack("<6f", bytes(self.emu.dp.read(viewports + 24 * i, 24))) for i in range(count)]

    def RSGetViewports(self, count, viewports):
        dp = self.emu.dp
        if not viewports:
            dp.write(count, struct.pack("<I", len(self.viewports)))
            return
        n = min(struct.unpack("<I", bytes(dp.read(count, 4)))[0], len(self.viewports))
        for i in range(n):
            dp.write(viewports + 24 * i, struct.pack("<6f", *self.viewports[i]))
        dp.write(count, struct.pack("<I", n))

    def RSSetScissorRects(self, count, rects):
        self.scissors = [struct.unpack("<4i", bytes(self.emu.dp.read(rects + 16 * i, 16))) for i in range(count)]

    def SOSetTargets(self, count, buffers, offsets):
        return None

    def Begin(self, query):
        return None

    def End(self, query):
        return None

    def GetData(self, query, data, size, flags):
        if data and size:
            self.emu.dp.write(data, b"\0" * size)
        return 0

    def SetPredication(self, predicate, value):
        return None

    def GenerateMips(self, view):
        return None

    def SetResourceMinLOD(self, resource, lod):
        return None

    # clears, copies, maps
    def ClearRenderTargetView(self, view, color):
        impl = self.emu.com.impl(view)
        if not isinstance(impl, View11) or impl.image is None:
            return
        rgba = struct.unpack("<4f", bytes(self.emu.dp.read(color, 16)))
        image = impl.image
        if image.raw is not None:
            _missing(self.emu.dp, f"Direct3D 11 render target of format {impl.resource.format}")
            return
        image.fb.clear(rgba)

    def ClearDepthStencilView(self, view, flags, depth, stencil):
        impl = self.emu.com.impl(view)
        if isinstance(impl, View11) and impl.image is not None and flags & 1:
            impl.image.fb.depth[:] = depth

    def ClearUnorderedAccessViewFloat(self, view, values):
        return None

    def ClearUnorderedAccessViewUint(self, view, values):
        return None

    def CopyResource(self, dst, src):
        target, source = self.emu.com.impl(dst), self.emu.com.impl(src)
        if not isinstance(target, Resource11) or not isinstance(source, Resource11):
            return
        if target.kind == "buffer" and source.kind == "buffer":
            self.emu.dp.write(target.address, source.data()[:target.size])
            return
        for a, b in zip(target.images, source.images):
            a.copy_from(b)

    def CopySubresourceRegion(self, dst, dst_sub, x, y, z, src, src_sub, box):
        target, source = self.emu.com.impl(dst), self.emu.com.impl(src)
        if not isinstance(target, Resource11) or not isinstance(source, Resource11):
            return
        dp = self.emu.dp
        area = struct.unpack("<6I", bytes(dp.read(box, 24))) if box else None
        if target.kind == "buffer":
            begin, end = (area[0], area[3]) if area else (0, source.size)
            dp.write(target.address + x, source.data()[begin:end])
            return
        if dst_sub < len(target.images) and src_sub < len(source.images):
            rect = (area[0], area[1], area[3], area[4]) if area else None
            target.images[dst_sub].copy_from(source.images[src_sub], rect, (x, y))

    def UpdateSubresource(self, dst, sub, box, data, row_pitch, depth_pitch):
        target = self.emu.com.impl(dst)
        if not isinstance(target, Resource11):
            return
        dp = self.emu.dp
        area = struct.unpack("<6I", bytes(dp.read(box, 24))) if box else None
        if target.kind == "buffer":
            begin, end = (area[0], area[3]) if area else (0, target.size)
            dp.write(target.address + begin, bytes(dp.read(data, end - begin)))
            return
        if sub >= len(target.images):
            return
        image = target.images[sub]
        rect = (area[0], area[1], area[3], area[4]) if area else (0, 0, image.width, image.height)
        image.from_bytes(bytes(dp.read(data, row_pitch * (rect[3] - rect[1]))), row_pitch, rect)

    def Map(self, resource, sub, kind, flags, mapped):
        target = self.emu.com.impl(resource)
        if not isinstance(target, Resource11):
            return E_INVALIDARG
        dp = self.emu.dp
        if target.kind == "buffer":
            address, pitch, depth_pitch = target.address, target.size, target.size
        else:
            if sub >= len(target.images):
                return E_INVALIDARG
            image = target.images[sub]
            address, pitch, depth_pitch = image.map(dp), image.pitch, image.pitch * image.height
            target.mapped_sub = sub
        dp.write_ptr(mapped, address)
        dp.write(mapped + dp.ptr_size(), struct.pack("<2I", pitch, depth_pitch))
        return 0

    def Unmap(self, resource, sub):
        target = self.emu.com.impl(resource)
        if isinstance(target, Resource11) and target.kind != "buffer" and sub < len(target.images):
            target.images[sub].unmap(self.emu.dp)

    # drawing
    def Draw(self, count, start):
        self._draw(list(range(start, start + count)), [0])

    def DrawInstanced(self, count, instances, start, start_instance):
        self._draw(list(range(start, start + count)), list(range(start_instance, start_instance + instances)))

    def _indices(self, count, start, base):
        if self.index_buffer is None or self.index_buffer[0] is None:
            return []
        buffer, fmt, offset = self.index_buffer
        size = 4 if fmt == 42 else 2
        data = buffer.data()
        at = offset + start * size
        count = max(0, min(count, (len(data) - at) // size))
        values = struct.unpack_from(f"<{count}{'I' if size == 4 else 'H'}", data, at)
        base = base - (1 << 32) if base & 0x80000000 else base
        return [v + base for v in values]

    def DrawIndexed(self, count, start, base):
        self._draw(self._indices(count, start, base), [0])

    def DrawIndexedInstanced(self, count, instances, start, base, start_instance):
        self._draw(self._indices(count, start, base), list(range(start_instance, start_instance + instances)))

    def DrawAuto(self):
        _missing(self.emu.dp, "Direct3D 11 DrawAuto")

    def DrawIndexedInstancedIndirect(self, buffer, offset):
        _missing(self.emu.dp, "Direct3D 11 indirect draws")

    def DrawInstancedIndirect(self, buffer, offset):
        _missing(self.emu.dp, "Direct3D 11 indirect draws")

    def _draw(self, vertex_ids: List[int], instances: List[int]):
        dp = self.emu.dp
        vs, ps = self.shaders.get("vs"), self.shaders.get("ps")
        for stage in ("gs", "hs", "ds"):
            if self.shaders.get(stage) is not None:
                _missing(dp, "Direct3D 11 geometry / tessellation shaders")
                return
        if vs is None or vs.data is None:
            _missing(dp, "Direct3D 11 draw without a vertex shader")
            return
        target = self.targets[0] if self.targets else None
        image = target.image if target is not None else None
        if image is None or not vertex_ids or not self.viewports:
            return
        if image.raw is not None:
            _missing(dp, f"Direct3D 11 render target of format {target.resource.format}")
            return
        from .dxbc import Unsupported
        try:
            for instance in instances:
                self._draw_instance(vs.data, ps.data if ps is not None else None, image, vertex_ids, instance)
        except Unsupported as e:
            _missing(dp, f"Direct3D 11 shader: {e}")

    def _constant_buffers(self, stage):
        out = {}
        for (s, slot), buffer in self.constant_buffers.items():
            if s == stage and buffer is not None:
                data = buffer.data()
                data += b"\0" * (-len(data) % 16)
                out[slot] = numpy.frombuffer(data, dtype=numpy.uint32).reshape(-1, 4)
        return out

    def _textures(self, stage):
        out, samplers = {}, {}
        for (s, slot), view in self.resources.items():
            if s == stage and view is not None and view.image is not None:
                out[slot] = view.image.texels()
        for (s, slot), state in self.sampler_states.items():
            if s == stage and state is not None and state.data:
                filter_type, address_u = struct.unpack_from("<2I", state.data, 0)
                samplers[slot] = (bool(filter_type & 0x4) or filter_type in (0x55, 0xD5),
                                  {1: "wrap", 2: "mirror", 3: "clamp", 4: "border", 5: "mirror"}.get(address_u, "clamp"))
        return out, samplers

    def _fetch(self, vs, vertex_ids, instance):
        """The input registers of the vertex shader (the input layout reading the vertex buffers)."""
        n = len(vertex_ids)
        inputs = {}
        layout = self.layout.data if self.layout is not None else []
        ids = numpy.array(vertex_ids, dtype=numpy.int64)
        cache = {}
        for element in vs.inputs:
            register = element["register"]
            if element["sv"] == 6:  # SV_VertexID
                inputs[register] = numpy.repeat(ids.astype(numpy.uint32)[:, None], 4, axis=1)
                continue
            if element["sv"] == 8:  # SV_InstanceID
                inputs[register] = numpy.full((n, 4), instance, dtype=numpy.uint32)
                continue
            match = next((e for e in layout if e["name"] == element["name"] and e["index"] == element["index"]), None)
            value = numpy.zeros((n, 4), dtype=numpy.float32)
            value[:, 3] = 1
            value = value.view(numpy.uint32)
            if match is not None:
                entry = self.vertex_buffers.get(match["slot"])
                if entry and entry[0] is not None:
                    buffer, stride, offset = entry
                    if match["slot"] not in cache:
                        cache[match["slot"]] = buffer.data()
                    data = cache[match["slot"]]
                    rows = ids if not match["instanced"] else numpy.full(n, instance // max(1, match["step"]))
                    value = _vertex_attribute(data, offset + match["offset"], stride, rows, match["format"])
            inputs[register] = value
        return inputs

    def _draw_instance(self, vs, ps, image: Image, vertex_ids, instance):
        unique, inverse = numpy.unique(numpy.array(vertex_ids, dtype=numpy.int64), return_inverse=True)
        inputs = self._fetch(vs, unique.tolist(), instance)
        textures, samplers = self._textures("vs")
        outputs, _, _ = vs.run(len(unique), inputs, self._constant_buffers("vs"), textures, samplers)
        position_register = next((e["register"] for e in vs.outputs if e["sv"] == 1), None)
        if position_register is None or position_register not in outputs:
            return
        clip = outputs[position_register].view(numpy.float32).astype(numpy.float64)
        fb = image.fb
        x, y, w, h, mn, mx = self.viewports[0]
        valid = clip[:, 3] > 1e-9
        safe_w = numpy.where(valid, clip[:, 3], 1)
        ndc = clip[:, :3] / safe_w[:, None]
        screen = numpy.stack([x + (ndc[:, 0] + 1) * w / 2, y + (1 - ndc[:, 1]) * h / 2, mn + ndc[:, 2] * (mx - mn),
                              1 / safe_w], axis=1)
        # the rasterizer state: fill, cull, front face, scissor
        fill, cull_mode, front_ccw, scissor_on = 3, 3, 0, 0
        if self.rasterizer is not None and self.rasterizer.data:
            fill, cull_mode, front_ccw = struct.unpack_from("<3I", self.rasterizer.data, 0)
            scissor_on = struct.unpack_from("<I", self.rasterizer.data, 28)[0]
        cull = 0 if cull_mode == 1 else ((1 if not front_ccw else 2) if cull_mode == 3 else (2 if not front_ccw else 1))
        scissor = None
        if scissor_on and self.scissors:
            scissor = self.scissors[0]
        depth_view = self.depth
        depth = depth_view.image.fb.depth if depth_view is not None and depth_view.image is not None and \
            depth_view.image.fb.depth.shape == fb.depth.shape else None
        depth_func, depth_write = 0, False
        if depth is not None:
            depth_func, depth_write = 2, True
            if self.depth_state is not None and self.depth_state.data:
                enabled, write_mask, func = struct.unpack_from("<3I", self.depth_state.data, 0)
                depth_func, depth_write = (func if enabled else 0), bool(enabled and write_mask)
        blend = None
        if self.blend is not None and self.blend.data:
            values = struct.unpack_from("<8I", self.blend.data, 8)
            blend = Blend(bool(values[0]), values[1], values[2], values[3], values[4], values[5], values[6], values[7] & 0xF)
        # varyings: the outputs of the vertex shader read by the pixel shader
        links = []
        if ps is not None:
            for element in ps.inputs:
                if element["sv"] == 1:
                    links.append((element["register"], "position", None))
                elif element["sv"] == 9:
                    links.append((element["register"], "front", None))
                elif element["sv"] == 0 or element["name"].startswith("SV_") is False:
                    source = vs.register_of(element["name"], element["index"], outputs=True)
                    flat = ps.interpolation.get(element["register"], 2) == 1 or element["kind"] in (1, 2)
                    links.append((element["register"], source, flat))
        target_register = next((e["register"] for e in ps.outputs if e["sv"] == 64 and e["index"] == 0), 0) \
            if ps is not None else None
        ps_cbs = self._constant_buffers("ps")
        ps_textures, ps_samplers = self._textures("ps")
        kind = {1: "points", 2: "lines", 3: "line_strip", 4: "triangles", 5: "strip"}.get(self.topology)
        if kind is None:
            _missing(self.emu.dp, f"Direct3D 11 topology {self.topology}")
            return
        for indices in primitives(kind, len(inverse)):
            corners = [int(inverse[i]) for i in indices]
            if not all(valid[c] for c in corners):
                continue
            if len(corners) != 3:
                if ps is None:
                    continue
                color = self._shade_vertex(ps, outputs, links, target_register, corners[0], screen, ps_cbs, ps_textures,
                                           ps_samplers)
                points = [(screen[c][0], screen[c][1]) for c in corners]
                (fb.lines(points, color) if len(corners) == 2 else fb.points(points, color))
                continue
            covered = fb.cover(tuple(screen[corners[0]][:3]), tuple(screen[corners[1]][:3]), tuple(screen[corners[2]][:3]),
                               cull, scissor)
            if covered is None or ps is None:
                if covered is not None and depth is not None:
                    y0, x0, inside, _, _, _, z = covered
                    output(fb, y0, x0, inside & False, numpy.zeros(inside.shape + (4,)), z, depth_func, depth_write,
                           None, depth=depth)
                continue
            y0, x0, inside, w0, w1, w2, z = covered
            if fill == 2:  # wireframe
                pass
            lanes = numpy.nonzero(inside)
            count = len(lanes[0])
            inv = [screen[c][3] for c in corners]
            b0, b1, b2 = w0[lanes] * inv[0], w1[lanes] * inv[1], w2[lanes] * inv[2]
            total = b0 + b1 + b2
            total[total == 0] = 1
            b0, b1, b2 = b0 / total, b1 / total, b2 / total
            ps_inputs = {}
            for register, source, flat in links:
                if source == "position":
                    value = numpy.stack([lanes[1] + x0 + 0.5, lanes[0] + y0 + 0.5, z[lanes], total * 0 + 1 / (
                        w0[lanes] * inv[0] + w1[lanes] * inv[1] + w2[lanes] * inv[2])], axis=1).astype(numpy.float32)
                    ps_inputs[register] = value.view(numpy.uint32)
                elif source == "front":
                    ps_inputs[register] = numpy.full((count, 4), 0xFFFFFFFF, dtype=numpy.uint32)
                elif source is not None and source in outputs:
                    values = outputs[source]
                    if flat:
                        ps_inputs[register] = numpy.broadcast_to(values[corners[0]], (count, 4)).copy()
                    else:
                        f = values.view(numpy.float32).astype(numpy.float64)
                        mixed = b0[:, None] * f[corners[0]] + b1[:, None] * f[corners[1]] + b2[:, None] * f[corners[2]]
                        ps_inputs[register] = mixed.astype(numpy.float32).view(numpy.uint32)
            result, alive, out_depth = ps.run(count, ps_inputs, ps_cbs, ps_textures, ps_samplers)
            color = result.get(target_register)
            if color is None:
                continue
            rgba = numpy.zeros(inside.shape + (4,), dtype=numpy.float32)
            rgba[lanes] = color.view(numpy.float32)
            mask = numpy.zeros(inside.shape, dtype=bool)
            mask[lanes] = alive
            if out_depth is not None:
                z = z.copy()
                z[lanes] = out_depth
            output(fb, y0, x0, mask, rgba, z, depth_func, depth_write, blend, self.blend_factor, depth=depth)

    def _shade_vertex(self, ps, outputs, links, target_register, corner, screen, cbs, textures, samplers):
        inputs = {}
        for register, source, flat in links:
            if source == "position":
                inputs[register] = numpy.array([screen[corner][:3].tolist() + [1.0]], dtype=numpy.float32).view(numpy.uint32)
            elif source not in (None, "front") and source in outputs:
                inputs[register] = outputs[source][corner:corner + 1].copy()
        result, _, _ = ps.run(1, inputs, cbs, textures, samplers)
        color = result.get(target_register)
        return color.view(numpy.float32)[0].tolist() if color is not None else [1.0, 1.0, 1.0, 1.0]


def _stage_methods():
    """XXSetConstantBuffers / XXSetShaderResources / XXSetSamplers and their Get of each stage."""
    for prefix in ("VS", "PS", "GS", "HS", "DS", "CS"):
        stage = prefix.lower()
        for kind, setter, table in (("ConstantBuffers", "_set_cbs", "constant_buffers"),
                                    ("ShaderResources", "_set_srvs", "resources"),
                                    ("Samplers", "_set_samplers", "sampler_states")):
            setattr(Context11, f"{prefix}Set{kind}", (lambda s, f: lambda self, start, count, array: getattr(self, f)(
                s, start, count, array))(stage, setter))
            setattr(Context11, f"{prefix}Get{kind}", (lambda s, t: lambda self, start, count, array: self._get(
                getattr(self, t), s, start, count, array))(stage, table))


_stage_methods()

VERTEX_FORMATS = {2: ("f", 4), 6: ("f", 3), 16: ("f", 2), 41: ("f", 1), 3: ("I", 4), 7: ("I", 3), 17: ("I", 2), 42: ("I", 1),
                  4: ("i", 4), 8: ("i", 3), 18: ("i", 2), 43: ("i", 1), 28: ("unorm8", 4), 87: ("bgra8", 4),
                  30: ("B", 4), 32: ("b8n", 4), 10: ("half", 4), 34: ("half", 2), 37: ("unorm16", 2), 11: ("unorm16", 4),
                  35: ("unorm16", 2), 13: ("h", 4), 38: ("h", 2), 14: ("snorm16", 4), 37 + 100: ("snorm16", 2)}


def _vertex_attribute(data: bytes, offset: int, stride: int, rows, fmt: int):
    """An attribute of the vertices rows as (n, 4) uint32 (the 32-bit values of the shader's input)."""
    n = len(rows)
    kind, count = VERTEX_FORMATS.get(fmt, ("f", 4))
    sizes = {"f": 4, "I": 4, "i": 4, "unorm8": 1, "bgra8": 1, "B": 1, "b8n": 1, "half": 2, "unorm16": 2, "h": 2, "snorm16": 2}
    size = sizes[kind] * count
    buffer = numpy.frombuffer(data, dtype=numpy.uint8)
    starts = offset + rows * stride
    valid = (starts >= 0) & (starts + size <= len(buffer))
    raw = numpy.zeros((n, size), dtype=numpy.uint8)
    if valid.any():
        raw[valid] = buffer[starts[valid][:, None] + numpy.arange(size)]
    out = numpy.zeros((n, 4), dtype=numpy.float32)
    out[:, 3] = 1
    if kind == "f":
        out[:, :count] = raw.view(numpy.float32).reshape(n, count)
    elif kind in ("I", "i"):
        result = numpy.zeros((n, 4), dtype=numpy.uint32)
        result[:, 3] = 1
        result[:, :count] = raw.view(numpy.uint32).reshape(n, count)
        return result
    elif kind == "B":
        result = numpy.zeros((n, 4), dtype=numpy.uint32)
        result[:, :count] = raw.reshape(n, count)
        return result
    elif kind == "unorm8":
        out[:, :count] = raw.reshape(n, count) / 255
    elif kind == "bgra8":
        out[:, :4] = raw.reshape(n, 4)[:, [2, 1, 0, 3]] / 255
    elif kind == "b8n":
        out[:, :count] = numpy.maximum(raw.view(numpy.int8).reshape(n, count) / 127, -1)
    elif kind == "half":
        out[:, :count] = raw.view(numpy.float16).reshape(n, count).astype(numpy.float32)
    elif kind == "unorm16":
        out[:, :count] = raw.view(numpy.uint16).reshape(n, count) / 65535
    elif kind == "snorm16":
        out[:, :count] = numpy.maximum(raw.view(numpy.int16).reshape(n, count) / 32767, -1)
    elif kind == "h":
        out[:, :count] = raw.view(numpy.int16).reshape(n, count)
    return out.view(numpy.uint32)


class Device11:
    interfaces = ["IDXGIDevice"]

    def __init__(self, emu, level, flags):
        self.emu, self.level, self.flags = emu, level, flags
        self.context = Context11(emu, self)

    def _out(self, impl, interface, out):
        if out:
            self.emu.dp.write_ptr(out, self.emu.com.create(impl, interface))
            return 0
        return S_FALSE

    def CreateBuffer(self, desc, initial, out):
        resource = Resource11(self.emu, "buffer", bytes(self.emu.dp.read(desc, 24)))
        resource.interface = "ID3D11Buffer"
        resource.initial(initial)
        return self._out(resource, "ID3D11Buffer", out)

    def CreateTexture2D(self, desc, initial, out):
        resource = Resource11(self.emu, "texture2d", bytes(self.emu.dp.read(desc, 44)))
        resource.interface = "ID3D11Texture2D"
        resource.interfaces = ["IDXGISurface"]
        resource.initial(initial)
        return self._out(resource, "ID3D11Texture2D", out)

    def CreateTexture1D(self, desc, initial, out):
        _missing(self.emu.dp, "Direct3D 11 1D textures")
        return E_NOTIMPL

    def CreateTexture3D(self, desc, initial, out):
        _missing(self.emu.dp, "Direct3D 11 3D textures")
        return E_NOTIMPL

    def _view(self, resource, desc, out, kind, interface, size):
        impl = self.emu.com.impl(resource)
        if not isinstance(impl, Resource11):
            return E_INVALIDARG
        view = View11(self.emu, impl, kind, bytes(self.emu.dp.read(desc, size)) if desc else None)
        return self._out(view, interface, out)

    def CreateRenderTargetView(self, resource, desc, out):
        return self._view(resource, desc, out, "rtv", "ID3D11RenderTargetView", 20)

    def CreateDepthStencilView(self, resource, desc, out):
        return self._view(resource, desc, out, "dsv", "ID3D11DepthStencilView", 24)

    def CreateShaderResourceView(self, resource, desc, out):
        return self._view(resource, desc, out, "srv", "ID3D11ShaderResourceView", 24)

    def CreateUnorderedAccessView(self, resource, desc, out):
        return self._view(resource, desc, out, "uav", "ID3D11UnorderedAccessView", 24)

    def CreateInputLayout(self, descs, count, bytecode, length, out):
        dp = self.emu.dp
        ptr = dp.ptr_size()
        size = 32 if ptr == 8 else 28
        elements, offsets = [], {}
        for i in range(count):
            base = descs + size * i
            name = dp.read_ptr(base)
            from .apihooks import read_astring
            index, fmt, slot, offset, classification, step = struct.unpack("<6I", bytes(dp.read(base + ptr, 24)))
            if offset == 0xFFFFFFFF:
                offset = offsets.get(slot, 0)
            kind, components = VERTEX_FORMATS.get(fmt, ("f", 4))
            width = {"f": 4, "I": 4, "i": 4, "half": 2, "unorm16": 2, "h": 2, "snorm16": 2}.get(kind, 1) * components
            offsets[slot] = offset + width
            elements.append(dict(name=(read_astring(dp, name) or "").upper(), index=index, format=fmt, slot=slot,
                                 offset=offset, instanced=classification == 1, step=step))
        return self._out(State11(self.emu, "layout", elements), "ID3D11InputLayout", out)

    def _shader(self, bytecode, length, out, interface):
        from .dxbc import Shader, Unsupported
        try:
            shader = Shader(bytes(self.emu.dp.read(bytecode, length)))
        except (Unsupported, struct.error, ValueError) as e:
            _missing(self.emu.dp, f"Direct3D 11 shader: {e}")
            shader = None
        return self._out(State11(self.emu, interface, shader), interface, out)

    def CreateVertexShader(self, bytecode, length, linkage, out):
        return self._shader(bytecode, length, out, "ID3D11VertexShader")

    def CreatePixelShader(self, bytecode, length, linkage, out):
        return self._shader(bytecode, length, out, "ID3D11PixelShader")

    def CreateGeometryShader(self, bytecode, length, linkage, out):
        return self._shader(bytecode, length, out, "ID3D11GeometryShader")

    def CreateGeometryShaderWithStreamOutput(self, bytecode, length, decl, entries, strides, count, stream, linkage, out):
        return self._shader(bytecode, length, out, "ID3D11GeometryShader")

    def CreateHullShader(self, bytecode, length, linkage, out):
        return self._shader(bytecode, length, out, "ID3D11HullShader")

    def CreateDomainShader(self, bytecode, length, linkage, out):
        return self._shader(bytecode, length, out, "ID3D11DomainShader")

    def CreateComputeShader(self, bytecode, length, linkage, out):
        return self._shader(bytecode, length, out, "ID3D11ComputeShader")

    def CreateClassLinkage(self, out):
        return self._out(State11(self.emu, "linkage"), "ID3D11ClassLinkage", out)

    def _state(self, desc, size, out, interface):
        return self._out(State11(self.emu, interface, bytes(self.emu.dp.read(desc, size))), interface, out)

    def CreateBlendState(self, desc, out):
        return self._state(desc, 8 + 32 * 8, out, "ID3D11BlendState")

    def CreateDepthStencilState(self, desc, out):
        return self._state(desc, 52, out, "ID3D11DepthStencilState")

    def CreateRasterizerState(self, desc, out):
        return self._state(desc, 40, out, "ID3D11RasterizerState")

    def CreateSamplerState(self, desc, out):
        return self._state(desc, 52, out, "ID3D11SamplerState")

    def CreateQuery(self, desc, out):
        return self._state(desc, 8, out, "ID3D11Query")

    def CreatePredicate(self, desc, out):
        return self._state(desc, 8, out, "ID3D11Predicate")

    def CreateCounter(self, desc, out):
        return E_INVALIDARG

    def CreateDeferredContext(self, flags, out):
        return E_NOTIMPL

    def CheckFormatSupport(self, fmt, out):
        self.emu.dp.write(out, struct.pack("<I", 0x3FFFFFF))

    def CheckMultisampleQualityLevels(self, fmt, count, out):
        self.emu.dp.write(out, struct.pack("<I", 1 if count == 1 else 0))

    def CheckFeatureSupport(self, feature, data, size):
        self.emu.dp.write(data, b"\0" * size)

    def CheckCounterInfo(self, info):
        self.emu.dp.write(info, b"\0" * 12)

    def SetPrivateData(self, guid, size, data):
        return 0

    def SetPrivateDataInterface(self, guid, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND

    def GetFeatureLevel(self):
        return self.level

    def GetCreationFlags(self):
        return self.flags

    def GetDeviceRemovedReason(self):
        return 0

    def GetImmediateContext(self, out):
        self.emu.dp.write_ptr(out, self.emu.com.create(self.context, "ID3D11DeviceContext"))
        self.context.refs += 1

    def SetExceptionMode(self, flags):
        return 0

    def GetExceptionMode(self):
        return 0

    # IDXGIDevice
    def GetAdapter(self, out):
        self.emu.dp.write_ptr(out, self.emu.com.create(self.emu.adapter, "IDXGIAdapter1"))

    def GetParent(self, riid, out):
        return self.emu.query(self.emu.adapter, riid, out)

    def SetGPUThreadPriority(self, priority):
        return 0

    def GetGPUThreadPriority(self, out):
        self.emu.dp.write(out, struct.pack("<i", 0))


class SwapChain11:
    def __init__(self, emu, device: Device11, desc: bytes):
        self.emu, self.device = emu, device
        dp = emu.dp
        self.desc = bytearray(desc)
        width, height = struct.unpack_from("<2I", desc, 0)
        self.format = struct.unpack_from("<I", desc, 16)[0]
        self.window = struct.unpack_from("<Q" if dp._x64 else "<I", desc, 48 if dp._x64 else 44)[0]
        if not width or not height:
            canvas = emu.canvas(self.window)
            width, height = canvas.size if canvas is not None else (640, 480)
            struct.pack_into("<2I", self.desc, 0, width, height)
        self._buffer(width, height)

    def _buffer(self, width, height):
        texture = struct.pack("<11I", width, height, 1, 1, self.format, 1, 0, 0, 0x20, 0, 0)
        self.back = Resource11(self.emu, "texture2d", texture)
        self.back.interface = "ID3D11Texture2D"
        self.back.interfaces = ["IDXGISurface"]

    def GetBuffer(self, index, riid, out):
        return self.emu.query(self.back, riid, out, "ID3D11Texture2D")

    def Present(self, interval, flags):
        if not flags & 1:  # DXGI_PRESENT_TEST
            self.emu.present(self.window, self.back.images[0])
        return 0

    def GetDesc(self, out):
        self.emu.dp.write(out, bytes(self.desc))

    def ResizeBuffers(self, count, width, height, fmt, flags):
        if not width or not height:
            canvas = self.emu.canvas(self.window)
            width, height = canvas.size if canvas is not None else (self.back.width, self.back.height)
        if fmt:
            self.format = fmt
            struct.pack_into("<I", self.desc, 16, fmt)
        struct.pack_into("<2I", self.desc, 0, width, height)
        self._buffer(width, height)

    def ResizeTarget(self, desc):
        return 0

    def SetFullscreenState(self, fullscreen, output):
        return 0

    def GetFullscreenState(self, fullscreen, output):
        dp = self.emu.dp
        if fullscreen:
            dp.write(fullscreen, struct.pack("<I", 0))
        if output:
            dp.write_ptr(output, 0)

    def GetContainingOutput(self, out):
        return DXGI_ERROR_NOT_FOUND

    def GetFrameStatistics(self, out):
        return DXGI_ERROR_NOT_FOUND

    def GetLastPresentCount(self, out):
        self.emu.dp.write(out, struct.pack("<I", 0))

    def GetParent(self, riid, out):
        return self.emu.query(self.emu.factory, riid, out)

    def GetDevice(self, riid, out):
        return self.emu.query(self.device, riid, out)

    def SetPrivateData(self, guid, size, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND


class Factory:
    interfaces = ["IDXGIFactory", "IDXGIFactory1"]

    def __init__(self, emu):
        self.emu = emu

    def EnumAdapters(self, index, out):
        if index:
            return DXGI_ERROR_NOT_FOUND
        self.emu.dp.write_ptr(out, self.emu.com.create(self.emu.adapter, "IDXGIAdapter"))
        self.emu.adapter.refs += 1

    def EnumAdapters1(self, index, out):
        if index:
            return DXGI_ERROR_NOT_FOUND
        self.emu.dp.write_ptr(out, self.emu.com.create(self.emu.adapter, "IDXGIAdapter1"))
        self.emu.adapter.refs += 1

    def MakeWindowAssociation(self, window, flags):
        return 0

    def GetWindowAssociation(self, out):
        self.emu.dp.write_ptr(out, 0)

    def CreateSwapChain(self, device, desc, out):
        impl = self.emu.com.impl(device)
        if not isinstance(impl, Device11):
            return E_INVALIDARG
        dp = self.emu.dp
        chain = SwapChain11(self.emu, impl, bytes(dp.read(desc, 72 if dp._x64 else 60)))
        dp.write_ptr(out, self.emu.com.create(chain, "IDXGISwapChain"))

    def CreateSoftwareAdapter(self, module, out):
        return E_NOTIMPL

    def IsCurrent(self):
        return 1

    def GetParent(self, riid, out):
        return E_NOINTERFACE

    def SetPrivateData(self, guid, size, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND


class Adapter:
    interfaces = ["IDXGIAdapter", "IDXGIAdapter1"]

    def __init__(self, emu):
        self.emu = emu

    def EnumOutputs(self, index, out):
        return DXGI_ERROR_NOT_FOUND

    def GetDesc(self, out):
        data = "Fathom software rasterizer".encode("utf-16-le").ljust(256, b"\0") + struct.pack("<4I", 0x1414, 0x8C, 0, 0)
        ptr = self.emu.dp.ptr_size()
        data += struct.pack("<3Q" if ptr == 8 else "<3I", 0, 256 << 20, 512 << 20) + struct.pack("<Ii", 1, 0)
        self.emu.dp.write(out, data)

    def GetDesc1(self, out):
        self.GetDesc(out)
        size = 256 + 16 + 3 * self.emu.dp.ptr_size() + 8
        self.emu.dp.write(out + size, struct.pack("<I", 2))  # DXGI_ADAPTER_FLAG_SOFTWARE

    def CheckInterfaceSupport(self, guid, version):
        return DXGI_ERROR_NOT_FOUND

    def GetParent(self, riid, out):
        return self.emu.query(self.emu.factory, riid, out)

    def SetPrivateData(self, guid, size, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND


# ------------------------------------------------------------------ the module
class Direct3D:
    def __init__(self, dp):
        self.dp = dp
        self.com = com_runtime(dp)
        self.devices9: List[Device9] = []
        self.device9: Optional[Device9] = None
        self.devices11: List[Device11] = []
        self.factory = Factory(self)
        self.adapter = Adapter(self)
        self.events: List[dict] = []
        self.presents = 0

    def canvas(self, window):
        gui = getattr(self.dp, "gui", None)
        drawing = getattr(gui, "drawing", None) if gui is not None else None
        return drawing.canvas(window) if drawing is not None and window else None

    def screen_size(self):
        gui = getattr(self.dp, "gui", None)
        return getattr(gui, "screen_size", (1920, 1080)) if gui is not None else (1920, 1080)

    def present(self, window, image: Image):
        self.presents += 1
        canvas = self.canvas(window)
        if canvas is None:
            return
        frame = image.fb.image()
        if frame.size != canvas.size:
            frame = frame.resize(canvas.size)
        canvas.paste(frame)

    def query(self, impl, riid, out, default=None) -> int:
        """QueryInterface on an implementation: the interface asked by riid among those it has."""
        iid = read_guid(self.dp, riid)
        names = [getattr(impl, "interface", None) or default] + list(getattr(impl, "interfaces", []))
        for name in names:
            if name and IIDS.get(name) == iid:
                self.dp.write_ptr(out, self.com.create(impl, name))
                impl.refs = getattr(impl, "refs", 0) + 1
                return 0
        if iid == "00000000-0000-0000-C000-000000000046" and names[0]:
            self.dp.write_ptr(out, self.com.create(impl, names[0]))
            return 0
        self.dp.write_ptr(out, 0)
        return E_NOINTERFACE

    def device_out(self, out):
        if self.device9 is None:
            return D3DERR_INVALIDCALL
        self.dp.write_ptr(out, self.com.create(self.device9, "IDirect3DDevice9"))
        self.device9.refs += 1
        return 0

    def device11_out(self, out):
        if not self.devices11:
            return E_FAIL
        self.dp.write_ptr(out, self.com.create(self.devices11[-1], "ID3D11Device"))
        self.devices11[-1].refs += 1
        return 0

    # -------------------------------------------------------------- exports
    def create9(self, dp, version):
        self.events.append(dict(kind="direct3d9"))
        return self.com.create(Direct3D9(self), "IDirect3D9")

    def create9ex(self, dp, version, out):
        dp.write_ptr(out, 0)
        return D3DERR_NOTAVAILABLE

    def create11(self, dp, adapter, driver, software, flags, levels, count, sdk, device_out, level_out, context_out,
                 chain_desc=0, chain_out=0):
        level = 0xB000
        if levels and count:
            requested = struct.unpack(f"<{count}I", bytes(dp.read(levels, 4 * count)))
            supported = [lvl for lvl in requested if lvl <= 0xB100]
            if not supported:
                return E_INVALIDARG
            level = supported[0]
        device = Device11(self, level, flags)
        self.devices11.append(device)
        if level_out:
            dp.write(level_out, struct.pack("<I", level))
        if device_out:
            dp.write_ptr(device_out, self.com.create(device, "ID3D11Device"))
        if context_out:
            dp.write_ptr(context_out, self.com.create(device.context, "ID3D11DeviceContext"))
        if chain_desc and chain_out:
            chain = SwapChain11(self, device, bytes(dp.read(chain_desc, 72 if dp._x64 else 60)))
            dp.write_ptr(chain_out, self.com.create(chain, "IDXGISwapChain"))
        self.events.append(dict(kind="direct3d11_device", level=hex(level)))
        return 0 if device_out or context_out or chain_out else S_FALSE

    def create11_chain(self, dp, adapter, driver, software, flags, levels, count, sdk, chain_desc, chain_out, device_out,
                       level_out, context_out):
        return self.create11(dp, adapter, driver, software, flags, levels, count, sdk, device_out, level_out, context_out,
                             chain_desc, chain_out)

    def create_factory(self, dp, riid, out):
        return self.query(self.factory, riid, out, "IDXGIFactory")


def install(dp):
    if numpy is None:
        return None
    emu = Direct3D(dp)
    dp.direct3d = emu
    hooks = dp.apihooks
    hooks.register_signature("d3d9.dll", "Direct3DCreate9", "p", emu.create9)
    hooks.register_signature("d3d9.dll", "Direct3DCreate9Ex", "pp", emu.create9ex)
    hooks.register_signature("d3d11.dll", "D3D11CreateDevice", "pppppppppp", emu.create11)
    hooks.register_signature("d3d11.dll", "D3D11CreateDeviceAndSwapChain", "pppppppppppp", emu.create11_chain)
    hooks.register_signature("dxgi.dll", "CreateDXGIFactory", "pp", emu.create_factory)
    hooks.register_signature("dxgi.dll", "CreateDXGIFactory1", "pp", emu.create_factory)
    hooks.register_signature("dxgi.dll", "CreateDXGIFactory2", "ppp", lambda dp, flags, riid, out: emu.create_factory(dp, riid, out))
    return emu
