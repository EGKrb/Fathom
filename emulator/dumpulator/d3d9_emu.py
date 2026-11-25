# Fathom addition to dumpulator: Direct3D 9 for the emulated program, drawn by raster.py. Boost Software
# License 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# d3d9.dll needs a graphics driver the emulator does not have: Direct3DCreate9 gives COM objects
# implemented here (apihooks.ComRuntime). The fixed-function pipeline (FVF or vertex declarations,
# world / view / projection transforms, viewport, lights, the texture stages with their operations,
# alpha test, culling, depth test, blending with separate alpha, flat / Gouraud shading, wireframe,
# sRGB writes), the vertex and pixel shaders (vs_1_1 to vs_3_0, ps_1_1 to ps_3_0, d3d9_shader.py) with
# their constants, vertex textures; DrawPrimitive(UP), DrawIndexedPrimitive(UP), vertex / index buffers,
# surfaces, 2D / cube / volume textures, several render targets, Clear, ColorFill, StretchRect,
# UpdateSurface, GetRenderTargetData, queries, Present to the window.

import struct
from typing import Dict, List, Optional

from . import d3d9_shader
from .apihooks import com_runtime, read_guid, E_NOTIMPL, E_NOINTERFACE
from .dxbc import Unsupported
from .dxgi_emu import dxgi_of, missing
from .gpu import D3D9, Image, Sampler, View, rows_of, sample_view, to_srgb
from .graphics_tables import IIDS
from .raster import Blend, Framebuffer, Output, Pipeline, Target, draw, numpy, primitives

E_INVALIDARG, E_OUTOFMEMORY, E_FAIL = 0x80070057, 0x8007000E, 0x80004005
D3DERR_NOTAVAILABLE, D3DERR_INVALIDCALL = 0x8876086A, 0x8876086C
S_FALSE = 1


def d3d9_kind(fmt: int) -> str:
    return D3D9.get(fmt, "bgra8")


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


def _decode9_unused(data: bytes, offset: int, kind: int):
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
        kind = d3d9_kind(fmt)
        self.levels = []
        count = levels or 1 + max(width, height).bit_length() - 1
        for level in range(count):
            image = Image(max(1, width >> level), max(1, height >> level), kind)
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
        kind = d3d9_kind(self.format)
        self.back = Surface9(self.emu, Image(width or 640, height or 480, kind), self.format, 0, 1)
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
        kind = d3d9_kind(fmt)
        surface = Surface9(self.emu, Image(width, height, kind), fmt, pool, usage)
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




# ------------------------------------------------------------------ textures of every kind, queries
def _texture_view(levels_per_slice, dimension):
    return View("texture", images=levels_per_slice, dimension=dimension, mips=len(levels_per_slice[0]) if levels_per_slice else 1)


def _texture9_view(self):
    return _texture_view([[surface.image for surface in self.levels]], "2d")


Texture9.view = _texture9_view
Texture9.interface = "IDirect3DTexture9"


class CubeTexture9:
    interface = "IDirect3DCubeTexture9"

    def __init__(self, emu, size, levels, usage, fmt, pool):
        self.emu, self.fmt, self.usage, self.pool = emu, fmt, usage, pool
        kind = d3d9_kind(fmt)
        count = levels or 1 + max(1, size).bit_length() - 1
        self.faces = [[Surface9(emu, Image(max(1, size >> m), max(1, size >> m), kind), fmt, pool, usage, self)
                       for m in range(count)] for _ in range(6)]

    def view(self):
        return _texture_view([[s.image for s in face] for face in self.faces], "cube")

    def GetLevelCount(self):
        return len(self.faces[0])

    def GetLevelDesc(self, level, desc):
        if level >= len(self.faces[0]):
            return D3DERR_INVALIDCALL
        return self.faces[0][level].GetDesc(desc)

    def GetCubeMapSurface(self, face, level, out):
        if face >= 6 or level >= len(self.faces[0]):
            return D3DERR_INVALIDCALL
        surface = self.faces[face][level]
        self.emu.dp.write_ptr(out, self.emu.com.create(surface, "IDirect3DSurface9"))
        surface.refs = getattr(surface, "refs", 1) + 1

    def LockRect(self, face, level, locked, rect, flags):
        if face >= 6 or level >= len(self.faces[0]):
            return D3DERR_INVALIDCALL
        return self.faces[face][level].LockRect(locked, rect, flags)

    def UnlockRect(self, face, level):
        if face < 6 and level < len(self.faces[0]):
            self.faces[face][level].UnlockRect()

    def GetType(self):
        return 5

    def GetDevice(self, out):
        return self.emu.device_out(out)

    def AddDirtyRect(self, face, rect):
        return 0

    def SetLOD(self, lod):
        return 0

    def GetLOD(self):
        return 0

    def GenerateMipSubLevels(self):
        return None


class VolumeTexture9:
    """A volume texture: its depth slices as images (LockBox writes them all)."""

    interface = "IDirect3DVolumeTexture9"

    def __init__(self, emu, width, height, depth, levels, usage, fmt, pool):
        self.emu, self.fmt, self.usage, self.pool = emu, fmt, usage, pool
        kind = d3d9_kind(fmt)
        self.slices = [Image(max(1, width), max(1, height), kind) for _ in range(max(1, depth))]
        self.width, self.height, self.depth = width, height, depth
        self.mapped = None

    def view(self):
        return _texture_view([[image] for image in self.slices], "3d")

    def GetLevelCount(self):
        return 1

    def LockBox(self, level, locked, box, flags):
        dp = self.emu.dp
        pitch = self.slices[0].pitch
        slice_pitch = pitch * rows_of(self.slices[0].kind, self.height)
        data = b"".join(image.to_bytes() for image in self.slices)
        self.mapped = dp.allocate(max(16, len(data)), page_align=True)
        dp.write(self.mapped, data)
        dp.write(locked, struct.pack("<2I", pitch, slice_pitch))
        dp.write_ptr(locked + 8, self.mapped)

    def UnlockBox(self, level):
        if self.mapped is None:
            return
        dp = self.emu.dp
        pitch = self.slices[0].pitch
        slice_pitch = pitch * rows_of(self.slices[0].kind, self.height)
        for z, image in enumerate(self.slices):
            image.from_bytes(bytes(dp.read(self.mapped + z * slice_pitch, slice_pitch)), pitch)

    def GetType(self):
        return 4

    def GetDevice(self, out):
        return self.emu.device_out(out)

    def SetLOD(self, lod):
        return 0

    def GetLOD(self):
        return 0


class Query9:
    interface = "IDirect3DQuery9"

    def __init__(self, emu, kind):
        self.emu, self.kind = emu, kind
        self.begin = self.end = 0

    def GetType(self):
        return self.kind

    def GetDataSize(self):
        return {8: 4, 9: 4, 10: 8, 11: 8, 12: 8}.get(self.kind, 4)

    def Issue(self, flags):
        fragments = self.emu.fragments
        if flags & 2:
            self.begin = fragments
        if flags & 1:
            self.end = fragments
        return 0

    def GetData(self, data, size, flags):
        if data and size:
            if self.kind == 9:      # OCCLUSION
                value = struct.pack("<I", max(0, self.end - self.begin))
            elif self.kind == 8:    # EVENT
                value = struct.pack("<I", 1)
            elif self.kind == 10:   # TIMESTAMP
                value = struct.pack("<Q", self.emu.fragments)
            elif self.kind == 12:   # TIMESTAMPFREQ
                value = struct.pack("<Q", 1000000000)
            else:
                value = b""
            self.emu.dp.write(data, value.ljust(size, b"\0")[:size])
        return 0

    def GetDevice(self, out):
        return self.emu.device_out(out)


# ------------------------------------------------------------------ the device: resources, shaders, constants
def _device_create_cube(self, size, levels, usage, fmt, pool, out, shared):
    self.emu.dp.write_ptr(out, self.emu.com.create(CubeTexture9(self.emu, size, levels, usage, fmt, pool), "IDirect3DCubeTexture9"))


def _device_create_volume(self, width, height, depth, levels, usage, fmt, pool, out, shared):
    texture = VolumeTexture9(self.emu, width, height, depth, levels, usage, fmt, pool)
    self.emu.dp.write_ptr(out, self.emu.com.create(texture, "IDirect3DVolumeTexture9"))


def _device_set_texture(self, stage, texture):
    impl = self.emu.com.impl(texture)
    self.textures[stage] = impl if hasattr(impl, "view") else None


def _device_get_texture(self, stage, out):
    texture = self.textures.get(stage)
    self.emu.dp.write_ptr(out, texture.com_address if texture is not None else 0)
    if texture is not None:
        texture.refs = getattr(texture, "refs", 1) + 1


def _device_create_query(self, kind, out):
    if kind not in (8, 9, 10, 11, 12):
        return D3DERR_NOTAVAILABLE
    if not out:
        return 0
    self.emu.dp.write_ptr(out, self.emu.com.create(Query9(self.emu, kind), "IDirect3DQuery9"))


def _shader_tokens(dp, address):
    """The tokens of a Direct3D 9 shader, up to its end token (comments skipped whole)."""
    out = bytearray()
    offset = 0
    while offset < 1 << 20:
        token = struct.unpack("<I", bytes(dp.read(address + offset, 4)))[0]
        out += struct.pack("<I", token)
        offset += 4
        if offset == 4:
            continue
        if token == 0x0000FFFF:
            break
        if token & 0xFFFF == 0xFFFE and not token & 0x80000000:
            length = 4 * ((token >> 16) & 0x7FFF)
            out += bytes(dp.read(address + offset, length))
            offset += length
    return bytes(out)


def _device_create_shader(kind):
    def create(self, function, out):
        state = Plain9(self.emu, kind)
        try:
            state.data = d3d9_shader.Shader9(_shader_tokens(self.emu.dp, function))
            if state.data.error:
                missing(self.emu.dp, f"Direct3D 9 shader: {state.data.error}")
        except (Unsupported, struct.error) as e:
            state.data = None
            missing(self.emu.dp, f"Direct3D 9 shader: {e}")
        interface = "IDirect3DVertexShader9" if kind == "vertex shader" else "IDirect3DPixelShader9"
        self.emu.dp.write_ptr(out, self.emu.com.create(state, interface))
    return create


def _device_constants(table, kind):
    def set_constants(self, start, data, count):
        dp = self.emu.dp
        array = getattr(self, table)
        if kind == "f":
            values = numpy.array(struct.unpack(f"<{4 * count}f", bytes(dp.read(data, 16 * count)))).reshape(count, 4)
        elif kind == "i":
            values = numpy.array(struct.unpack(f"<{4 * count}i", bytes(dp.read(data, 16 * count)))).reshape(count, 4)
        else:
            values = numpy.array(struct.unpack(f"<{count}I", bytes(dp.read(data, 4 * count)))) != 0
        end = min(len(array), start + count)
        if end > start:
            array[start:end] = values[:end - start]

    def get_constants(self, start, data, count):
        dp = self.emu.dp
        array = getattr(self, table)[start:start + count]
        if kind == "f":
            dp.write(data, struct.pack(f"<{array.size}f", *array.flatten()))
        elif kind == "i":
            dp.write(data, struct.pack(f"<{array.size}i", *array.flatten()))
        else:
            dp.write(data, struct.pack(f"<{array.size}I", *array.astype(int)))
    return set_constants, get_constants


Device9.CreateCubeTexture = _device_create_cube
Device9.CreateVolumeTexture = _device_create_volume
Device9.SetTexture = _device_set_texture
Device9.GetTexture = _device_get_texture
Device9.CreateQuery = _device_create_query
Device9.CreateVertexShader = _device_create_shader("vertex shader")
Device9.CreatePixelShader = _device_create_shader("pixel shader")
for _table, _kind, _set, _get in (("vs_float", "f", "SetVertexShaderConstantF", "GetVertexShaderConstantF"),
                                  ("vs_int", "i", "SetVertexShaderConstantI", "GetVertexShaderConstantI"),
                                  ("vs_bool", "b", "SetVertexShaderConstantB", "GetVertexShaderConstantB"),
                                  ("ps_float", "f", "SetPixelShaderConstantF", "GetPixelShaderConstantF"),
                                  ("ps_int", "i", "SetPixelShaderConstantI", "GetPixelShaderConstantI"),
                                  ("ps_bool", "b", "SetPixelShaderConstantB", "GetPixelShaderConstantB")):
    _setter, _getter = _device_constants(_table, _kind)
    setattr(Device9, _set, _setter)
    setattr(Device9, _get, _getter)

_device_init = Device9.__init__


def _device9_init(self, emu, d3d, window, flags, parameters):
    _device_init(self, emu, d3d, window, flags, parameters)
    self.vs_float = numpy.zeros((256, 4))
    self.vs_int = numpy.zeros((16, 4), dtype=numpy.int64)
    self.vs_bool = numpy.zeros(16, dtype=bool)
    self.ps_float = numpy.zeros((224, 4))
    self.ps_int = numpy.zeros((16, 4), dtype=numpy.int64)
    self.ps_bool = numpy.zeros(16, dtype=bool)
    self.targets = {0: self.target}
    if self.auto_depth:
        self.depth_surface = Surface9(emu, Image(self.back.image.width, self.back.image.height, "d24s8"), 75, 0, 2)


Device9.__init__ = _device9_init


def _device_set_render_target(self, index, surface):
    target = self._surface_of(surface)
    if index == 0:
        if target is None:
            return D3DERR_INVALIDCALL
        self.target = target
        self.viewport = [0, 0, target.image.width, target.image.height, 0.0, 1.0]
    if target is None:
        self.targets.pop(index, None)
    else:
        self.targets[index] = target


Device9.SetRenderTarget = _device_set_render_target


def _device_clear(self, count, rects, flags, color, z, stencil):
    dp = self.emu.dp
    x, y, w, h = self.viewport[:4]
    areas = [struct.unpack_from("<4i", bytes(dp.read(rects + 16 * i, 16))) for i in range(count)] if rects and count \
        else [(x, y, x + w, y + h)]
    rgba = _argb(color)
    if self.render_states.get(194, 0):
        rgba = list(to_srgb(numpy.array([rgba], dtype=numpy.float32))[0])
    for area in areas:
        if flags & 1:
            for target in self.targets.values():
                target.image.clear(rgba, area)
        if flags & 2:
            depth = self._depth_buffer()
            if depth is not None:
                x0, y0, x1, y1 = area
                depth[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = z


def _device_depth_buffer(self):
    """The depth values of the draws: the depth surface's (of the render target's size)."""
    surface = self.depth_surface
    if surface is None:
        return None
    depth = surface.image.fb.depth
    if depth.shape != (self.target.image.height, self.target.image.width):
        return None
    surface.image.changed()
    return depth


def _device_color_fill(self, surface, rect, color):
    target = self._surface_of(surface)
    if target is None:
        return D3DERR_INVALIDCALL
    area = struct.unpack("<4i", bytes(self.emu.dp.read(rect, 16))) if rect else None
    target.image.clear(_argb(color), area)


def _device_stretch_rect(self, src, src_rect, dst, dst_rect, filter_type):
    source, target = self._surface_of(src), self._surface_of(dst)
    if source is None or target is None:
        return D3DERR_INVALIDCALL
    dp = self.emu.dp
    a = struct.unpack("<4i", bytes(dp.read(src_rect, 16))) if src_rect else (0, 0, source.image.width, source.image.height)
    b = struct.unpack("<4i", bytes(dp.read(dst_rect, 16))) if dst_rect else (0, 0, target.image.width, target.image.height)
    if source.image.is_depth or target.image.is_depth:
        target.image.copy_from(source.image)
        return 0
    from PIL import Image as PilImage
    part = source.image.texels()[a[1]:a[3], a[0]:a[2]]
    width, height = b[2] - b[0], b[3] - b[1]
    if width <= 0 or height <= 0 or part.size == 0:
        return D3DERR_INVALIDCALL
    picture = PilImage.fromarray(numpy.round(numpy.clip(part, 0, 1) * 255).astype(numpy.uint8), "RGBA")
    picture = picture.resize((width, height), PilImage.BILINEAR if filter_type >= 2 else PilImage.NEAREST)
    texels = target.image.texels().copy()
    region = numpy.array(picture).astype(numpy.float32) / 255
    texels[b[1]:b[3], b[0]:b[2]] = region[:texels.shape[0] - b[1], :texels.shape[1] - b[0]]
    target.image.set_texels(texels)


Device9.Clear = _device_clear
Device9._depth_buffer = _device_depth_buffer
Device9.ColorFill = _device_color_fill
Device9.StretchRect = _device_stretch_rect


def _surface_get_dc(self, out):
    gui = getattr(self.emu.dp, "gui", None)
    drawing = getattr(gui, "drawing", None)
    if drawing is None or not drawing.enabled:
        return E_NOTIMPL
    from .gui_draw import GdiObject
    hdc = drawing.new_dc(0)
    bitmap = GdiObject("bitmap", image=self.image.picture())
    drawing.dcs[hdc].bitmap = bitmap
    self.dc = (hdc, bitmap)
    self.emu.dp.write_ptr(out, hdc)


def _surface_release_dc(self, hdc):
    dc = getattr(self, "dc", None)
    if dc is None:
        return D3DERR_INVALIDCALL
    if dc[1].image is not None:
        rgb = numpy.array(dc[1].image.convert("RGB")).astype(numpy.float32) / 255
        texels = self.image.texels().copy()
        texels[..., :3] = rgb
        self.image.set_texels(texels)
    self.emu.dp.gui.drawing.dcs.pop(dc[0], None)
    self.dc = None


Surface9.GetDC = _surface_get_dc
Surface9.ReleaseDC = _surface_release_dc


# ------------------------------------------------------------------ drawing
DECL_SIZES = {0: 4, 1: 8, 2: 12, 3: 16, 4: 4, 5: 4, 6: 4, 7: 8, 8: 4, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 14: 4, 15: 4, 16: 8}


def _attribute9(data, offset, stride, rows, kind):
    """A vertex element (D3DDECLTYPE) of the vertices rows: (n, 4) floats, (0, 0, 0, 1) for what it lacks."""
    rows = numpy.asarray(rows, dtype=numpy.int64)
    n = len(rows)
    size = DECL_SIZES.get(kind, 4)
    buffer = numpy.frombuffer(data, dtype=numpy.uint8) if not isinstance(data, numpy.ndarray) else data
    starts = offset + rows * stride
    valid = (starts >= 0) & (starts + size <= len(buffer))
    raw = numpy.zeros((n, size), dtype=numpy.uint8)
    if valid.any():
        raw[valid] = buffer[starts[valid][:, None] + numpy.arange(size)]
    out = numpy.zeros((n, 4))
    out[:, 3] = 1
    if kind <= 3:
        out[:, :kind + 1] = raw.view("<f4").reshape(n, kind + 1)
    elif kind == 4:   # D3DCOLOR: B G R A bytes
        out[:] = raw[:, [2, 1, 0, 3]] / 255.0
    elif kind == 5:
        out[:] = raw
    elif kind == 8:
        out[:] = raw / 255.0
    elif kind in (6, 7):
        count = 2 if kind == 6 else 4
        out[:, :count] = raw.view("<i2").reshape(n, count)
    elif kind in (9, 10):
        count = 2 if kind == 9 else 4
        out[:, :count] = numpy.maximum(raw.view("<i2").reshape(n, count) / 32767.0, -1)
    elif kind in (11, 12):
        count = 2 if kind == 11 else 4
        out[:, :count] = raw.view("<u2").reshape(n, count) / 65535.0
    elif kind in (13, 14):
        v = raw.view("<u4").reshape(n).astype(numpy.int64)
        parts = [(v >> s) & 1023 for s in (0, 10, 20)]
        if kind == 14:
            parts = [numpy.where(p >= 512, p - 1024, p) / 511.0 for p in parts]
        out[:, 0], out[:, 1], out[:, 2] = parts
    elif kind in (15, 16):
        count = 2 if kind == 15 else 4
        out[:, :count] = raw.view("<f2").reshape(n, count).astype(numpy.float64)
    return out


TOPOLOGY9 = {1: "points", 2: "lines", 3: "line_strip", 4: "triangles", 5: "strip", 6: "fan"}


def _sampler9(self, stage):
    """A gpu.Sampler from the sampler states of a stage."""
    get = lambda kind, default: self.samplers.get((stage, kind), default)
    modes = {1: "wrap", 2: "mirror", 3: "clamp", 4: "border", 5: "mirror"}
    border = _argb(get(4, 0))
    mip_filter = get(7, 0)                                  # D3DSAMP_MIPFILTER: none, point, linear
    bias = struct.unpack("<f", struct.pack("<I", get(8, 0) & 0xFFFFFFFF))[0]   # D3DSAMP_MIPMAPLODBIAS
    return Sampler(get(5, 1) >= 2, tuple(modes.get(get(k, 1), "wrap") for k in (1, 2, 3)), tuple(border),
                   mip_linear=mip_filter == 2, min_lod=float(get(9, 0)),   # D3DSAMP_MAXMIPLEVEL: the largest level
                   max_lod=0.0 if mip_filter == 0 else 1e9, lod_bias=bias)


def _texture_function(self, stage):
    texture = self.textures.get(stage)
    if texture is None:
        return None
    view = texture.view()
    view.srgb = bool(self.samplers.get((stage, 11), 0))
    sampler = self._sampler9(stage)
    dimension = view.dimension

    def function(coords, lod=None, bias=None, gradients=None):
        columns = 3 if dimension in ("cube", "3d") else 2
        if gradients is not None:
            gradients = tuple(numpy.asarray(g, dtype=numpy.float64)[:, :columns] for g in gradients)
        return sample_view(view, sampler, numpy.asarray(coords, dtype=numpy.float64)[:, :columns], lod, None, (0, 0), bias,
                           gradients)
    function.mipmaps = len(view.images[0]) > 1 and self.samplers.get((stage, 7), 0) != 0 if view.images else False
    return function


def _device_light(self, positions, normals, diffuse, specular, world_view):
    """The fixed-function lighting of the vertices (directional, point and spot lights as point lights)."""
    rs = self.render_states
    material = self.material
    n = len(positions)
    sources = {0: None, 1: diffuse, 2: specular}
    pick = lambda state, default_material: sources.get(rs.get(state, 0)) if rs.get(141, 1) and \
        sources.get(rs.get(state, 0)) is not None else numpy.broadcast_to(numpy.array(default_material), (n, 4))
    diffuse_source = pick(145, material[0:4]) if rs.get(145, 1) else numpy.broadcast_to(numpy.array(material[0:4]), (n, 4))
    ambient_source = pick(147, material[4:8])
    emissive_source = pick(148, material[12:16])
    ambient_light = numpy.array(_argb(rs.get(139, 0))[:3])
    color = emissive_source[:, :3] + ambient_source[:, :3] * ambient_light
    if normals is not None:
        normal = numpy.concatenate([normals[:, :3], numpy.zeros((n, 1))], 1) @ world_view
        length = numpy.linalg.norm(normal[:, :3], axis=1, keepdims=True)
        normal = normal[:, :3] / numpy.where(length == 0, 1, length)
        eye = (positions @ world_view)[:, :3]
        view = self.transforms.get(2, numpy.identity(4))
        for index, on in self.lights_on.items():
            if not on:
                continue
            light = self.lights[index]
            light_diffuse, light_ambient = numpy.array(light[1:4]), numpy.array(light[9:12])
            if light[0] == 3:
                direction = -(numpy.array(light[16:19] + [0.0]) @ view)[:3]
                direction = numpy.broadcast_to(direction / (numpy.linalg.norm(direction) or 1), (n, 3))
            else:
                direction = (numpy.array(light[13:16] + [1.0]) @ view)[:3] - eye
                direction = direction / numpy.maximum(numpy.linalg.norm(direction, axis=1, keepdims=True), 1e-12)
            color = color + ambient_source[:, :3] * light_ambient
            color = color + diffuse_source[:, :3] * light_diffuse * numpy.maximum(0, (normal * direction).sum(1, keepdims=True))
    return numpy.concatenate([numpy.clip(color, 0, 1), diffuse_source[:, 3:4]], 1)


def _stage_argument(arg, values, stage_texture):
    base = arg & 0xF
    value = values.get({0: "diffuse", 1: "current", 2: "texture", 3: "factor", 4: "specular", 5: "temp",
                        6: "constant"}.get(base, "current"))
    if base == 2:
        value = stage_texture
    if arg & 0x20:
        value = numpy.repeat(value[:, 3:4], 4, axis=1)
    if arg & 0x10:
        value = 1 - value
    return value


def _stage_operation(op, a1, a2, a0, values, texture):
    if op == 2:
        return a1
    if op == 3:
        return a2
    if op == 4:
        return a1 * a2
    if op == 5:
        return 2 * a1 * a2
    if op == 6:
        return 4 * a1 * a2
    if op == 7:
        return a1 + a2
    if op == 8:
        return a1 + a2 - 0.5
    if op == 9:
        return 2 * (a1 + a2 - 0.5)
    if op == 10:
        return a1 - a2
    if op == 11:
        return a1 + a2 - a1 * a2
    if op in (12, 13, 14, 16):
        alpha = {12: values["diffuse"], 13: texture, 14: values["factor"], 16: values["current"]}[op][:, 3:4]
        return a1 * alpha + a2 * (1 - alpha)
    if op == 15:
        return a1 + a2 * (1 - texture[:, 3:4])
    if op == 18:
        return numpy.concatenate([a1[:, :3] + a1[:, 3:4] * a2[:, :3], a1[:, 3:4]], 1)
    if op == 19:
        return numpy.concatenate([a1[:, :3] * a2[:, :3] + a1[:, 3:4], a1[:, 3:4]], 1)
    if op == 20:
        return numpy.concatenate([(1 - a1[:, 3:4]) * a2[:, :3] + a1[:, :3], a1[:, 3:4]], 1)
    if op == 21:
        return numpy.concatenate([(1 - a1[:, :3]) * a2[:, :3] + a1[:, 3:4], a1[:, 3:4]], 1)
    if op == 24:
        dot = (4 * (a1[:, :3] - 0.5) * (a2[:, :3] - 0.5)).sum(1, keepdims=True)
        return numpy.repeat(numpy.clip(dot, 0, 1), 4, axis=1)
    if op == 25:
        return a1 * a2 + a0
    if op == 26:
        return a0 * a1 + (1 - a0) * a2
    return a1


def _device_stages(self, attrs, columns, n):
    """The fixed-function texture stages: the color of the fragments."""
    def column(semantic, default):
        index = columns.get(semantic)
        if index is None:
            return numpy.broadcast_to(numpy.array(default, dtype=numpy.float64), (n, 4)).copy()
        return attrs[:, index:index + 4]

    tss = self.texture_stages
    values = {"diffuse": column((10, 0), (1.0, 1.0, 1.0, 1.0)), "specular": column((10, 1), (0.0, 0.0, 0.0, 0.0)),
              "factor": numpy.broadcast_to(numpy.array(_argb(self.render_states.get(60, 0xFFFFFFFF))), (n, 4)),
              "temp": numpy.zeros((n, 4))}
    values["current"] = values["diffuse"]
    for stage in range(8):
        color_op = tss.get((stage, 1), 4 if stage == 0 else 1)
        if color_op == 1:
            break
        alpha_op = tss.get((stage, 4), 2 if stage == 0 else 1)
        function = self._texture_function(stage)
        texcoord = tss.get((stage, 11), stage) & 0xFFFF
        if function is not None:
            texture = numpy.asarray(function(column((5, texcoord), (0.0, 0.0, 0.0, 1.0))), dtype=numpy.float64)
        else:
            texture = numpy.ones((n, 4))
        values["constant"] = numpy.broadcast_to(numpy.array(_argb(tss.get((stage, 32), 0xFFFFFFFF))), (n, 4))
        a = lambda key, default: _stage_argument(tss.get((stage, key), default), values, texture)
        color = _stage_operation(color_op, a(2, 2), a(3, 1), a(26, 1), values, texture)
        if alpha_op == 1:
            alpha = values["current"]
        else:
            alpha = _stage_operation(alpha_op, a(5, 2), a(6, 1), a(27, 1), values, texture)
        result = numpy.clip(numpy.concatenate([color[:, :3], alpha[:, 3:4]], 1), 0, 1)
        values["temp" if tss.get((stage, 28), 1) == 5 else "current"] = result
    color = values["current"]
    if self.render_states.get(29, 0):  # D3DRS_SPECULARENABLE
        color = numpy.concatenate([numpy.clip(color[:, :3] + values["specular"][:, :3], 0, 1), color[:, 3:]], 1)
    return color


ALPHA_TESTS = {1: lambda a, r: numpy.zeros(a.shape, bool), 2: numpy.less, 3: numpy.equal, 4: numpy.less_equal,
               5: numpy.greater, 6: numpy.not_equal, 7: numpy.greater_equal, 8: lambda a, r: numpy.ones(a.shape, bool)}


def _device_draw(self, kind: int, streams, index_list):
    from . import d3d9_shader
    dp = self.emu.dp
    rs = self.render_states
    ids = numpy.asarray(index_list, dtype=numpy.int64)
    if not len(ids):
        return 0
    vs_state, ps_state = self.vertex_shader, self.pixel_shader
    vs = vs_state.data if isinstance(vs_state, Plain9) else None
    ps = ps_state.data if isinstance(ps_state, Plain9) else None
    if (vs_state is not None and vs is None) or (ps_state is not None and ps is None):
        return 0
    unique, inverse = numpy.unique(ids, return_inverse=True)
    n = len(unique)
    values = {}
    for stream, offset, element_kind, usage, usage_index in self._elements():
        source = streams.get(stream)
        if source is not None:
            data, base, stride = source
            values[(usage, usage_index)] = _attribute9(data, base + offset, stride, unique, element_kind)
    semantics = {}
    clip = screen = None
    try:
        if vs is not None:
            inputs = {}
            for register, semantic in vs.input_semantics().items():
                if semantic in values:
                    inputs[register] = values[semantic]
            textures = {s: self._texture_function(257 + s) for s in range(4)}
            machine = d3d9_shader.run(vs, n, inputs, self.vs_float, self.vs_int, self.vs_bool, textures)
            for register, semantic in vs.output_semantics().items():
                if register in machine.outputs:
                    semantics[semantic] = machine.outputs[register]
            clip = semantics.pop((0, 0), None)
            if clip is None:
                return 0
        else:
            world, view, projection = self.transforms.get(256), self.transforms.get(2), self.transforms.get(3)
            diffuse = values.get((10, 0))
            specular = values.get((10, 1))
            if (9, 0) in values:   # POSITIONT: already on the screen (pixel centers at integers)
                pre = values[(9, 0)]
                rhw = numpy.where(pre[:, 3] == 0, 1, pre[:, 3])
                screen = numpy.stack([pre[:, 0] - 0.5, pre[:, 1] - 0.5, pre[:, 2], rhw], 1)
            else:
                position = values.get((0, 0), numpy.zeros((n, 4)) + [0, 0, 0, 1]).copy()
                position[:, 3] = 1
                clip = position @ (world @ view @ projection)
                if rs.get(137, 1):
                    diffuse = self._light(position, values.get((3, 0)), diffuse if diffuse is not None else
                                          numpy.ones((n, 4)), specular if specular is not None else numpy.zeros((n, 4)),
                                          world @ view)
            semantics[(10, 0)] = diffuse if diffuse is not None else numpy.ones((n, 4))
            if specular is not None:
                semantics[(10, 1)] = specular
            for (usage, index), value in values.items():
                if usage == 5:
                    semantics[(5, index)] = value
    except Unsupported as e:
        missing(dp, f"Direct3D 9 shader: {e}")
        return 0
    keys = sorted(semantics)
    columns = {key: 4 * i for i, key in enumerate(keys)}
    attrs = numpy.concatenate([semantics[key] for key in keys], axis=1) if keys else numpy.zeros((n, 0))
    flat = rs.get(9, 2) == 1
    flat_mask = numpy.array([flat and key[0] == 10 for key in keys for _ in range(4)], dtype=bool) if keys else None
    prims = TOPOLOGY9.get(kind, "points")
    from .raster import primitives as make_primitives, kind_of
    prims_array = make_primitives(prims, len(inverse))
    prims_array = inverse[prims_array] if len(prims_array) else prims_array
    raster_kind = kind_of(prims)
    vx, vy, vw, vh, vmin, vmax = self.viewport
    cull = {1: "none", 2: "front", 3: "back"}.get(rs.get(22, 3), "back")
    scissor = None
    if rs.get(174, 0):
        left, top, right, bottom = getattr(self, "scissor", (0, 0, self.target.image.width, self.target.image.height))
        scissor = (left, top, right, bottom)
    point_size = struct.unpack("<f", struct.pack("<I", rs.get(154, 0x3F800000) & 0xFFFFFFFF))[0]
    pipeline = Pipeline((vx, vy, vw, vh), "d3d", (vmin, vmax), offset=0.5, cull=cull, front_cw=True, provoking="first",
                        scissor=scissor, point_size=point_size)
    src, dst, op = rs.get(19, 2), rs.get(20, 1), rs.get(171, 1)
    if src == 12:
        src, dst = 5, 6
    elif src == 13:
        src, dst = 6, 5
    if rs.get(206, 0):
        src_a, dst_a, op_a = rs.get(207, 2), rs.get(208, 1), rs.get(209, 1)
    else:
        src_a, dst_a, op_a = src, dst, op
    masks = {0: 168, 1: 190, 2: 191, 3: 192}
    srgb = bool(rs.get(194, 0))
    targets = []
    for index in sorted(self.targets):
        surface = self.targets[index]
        blend = Blend(bool(rs.get(27, 0)), src, dst, op, src_a, dst_a, op_a, rs.get(masks.get(index, 168), 0xF) & 0xF)
        targets.append(Target(surface.image.color, blend, srgb=srgb))
        surface.image.changed()
    depth = self._depth_buffer()
    output = Output(targets, depth, rs.get(23, 4) if rs.get(7, 0) and depth is not None else 0, bool(rs.get(14, 1)),
                    _argb(rs.get(193, 0xFFFFFFFF)))
    alpha_test = rs.get(15, 0)
    alpha_ref, alpha_func = (rs.get(24, 0) & 0xFF) / 255.0, rs.get(25, 8)
    ps_textures = {s: self._texture_function(s) for s in range(16)} if ps is not None else {}
    ps_inputs = ps.input_semantics() if ps is not None else {}
    target_count = len(targets)

    def fragment(frags):
        alive = None
        depth_out = None
        if ps is not None:
            inputs = {}
            for register, semantic in ps_inputs.items():
                index = columns.get(semantic)
                if index is not None:
                    inputs[register] = frags.attrs[:, index:index + 4]
            system = {"vpos": numpy.stack([frags.x.astype(float), frags.y.astype(float), numpy.zeros(frags.n),
                                           numpy.zeros(frags.n)], 1),
                      "vface": numpy.repeat(numpy.where(frags.front, 1.0, -1.0)[:, None], 4, 1)}
            machine = d3d9_shader.run(ps, frags.n, inputs, self.ps_float, self.ps_int, self.ps_bool, ps_textures, system)
            if ps.major < 2:
                colors = [machine.temps.get(0, numpy.zeros((frags.n, 4)))]
            else:
                colors = [machine.outputs.get((d3d9_shader.COLOROUT, k)) for k in range(target_count)]
            alive = ~machine.killed
            depth_out = machine.depth
        else:
            colors = [self._stages(frags.attrs, columns, frags.n)]
        colors = colors + [None] * (target_count - len(colors))
        if alpha_test and colors and colors[0] is not None:
            passed = ALPHA_TESTS.get(alpha_func, ALPHA_TESTS[8])(numpy.round(colors[0][:, 3] * 255) / 255, alpha_ref)
            alive = passed if alive is None else alive & passed
        return colors[:target_count], alive, depth_out

    # 2x2 quads for dsx / dsy and the mip levels of mipmapped textures
    textures = ps_textures if ps is not None else {s: self._texture_function(s) for s in range(8)}
    mipmaps = any(getattr(f, "mipmaps", False) for f in textures.values() if f is not None)
    fragment.quads = raster_kind == "triangles" and (mipmaps or ps is not None and ps.uses_derivatives())
    fragment.parallel = True        # no side effect: the fragments may be shaded by several threads (raster.py)
    if rs.get(8, 3) == 2 and raster_kind == "triangles":   # wireframe
        prims_array = numpy.concatenate([prims_array[:, [0, 1]], prims_array[:, [1, 2]], prims_array[:, [2, 0]]])
        raster_kind = "lines"
    try:
        written = draw(output, pipeline, raster_kind, prims_array, attrs, fragment, clip=clip, screen=screen,
                       flat_mask=flat_mask)
    except Unsupported as e:
        missing(dp, f"Direct3D 9 shader: {e}")
        return 0
    self.emu.fragments += written
    return 0


Device9._draw = _device_draw
Device9._light = _device_light
Device9._stages = _device_stages
Device9._sampler9 = _sampler9
Device9._texture_function = _texture_function



class Direct3D9Root:
    """The Direct3D 9 of a process: its devices; the windows and COM through DXGI's helpers."""

    def __init__(self, dp):
        self.dp = dp
        self.dxgi = dxgi_of(dp)
        self.com = com_runtime(dp)
        self.devices9: List[Device9] = []
        self.device9: Optional[Device9] = None
        self.events = self.dxgi.events
        self.fragments = 0

    def canvas(self, window):
        return self.dxgi.canvas(window)

    def screen_size(self):
        return self.dxgi.screen_size()

    def present(self, window, image):
        self.dxgi.present(window, image)

    def query(self, impl, riid, out, default=None) -> int:
        return self.dxgi.query(impl, riid, out, default)

    def device_out(self, out):
        if self.device9 is None:
            return D3DERR_INVALIDCALL
        self.dp.write_ptr(out, self.com.create(self.device9, "IDirect3DDevice9"))
        self.device9.refs = getattr(self.device9, "refs", 1) + 1
        return 0

    def create9(self, dp, version):
        self.events.append(dict(kind="direct3d9"))
        return self.com.create(Direct3D9(self), "IDirect3D9")

    def create9ex(self, dp, version, out):
        dp.write_ptr(out, 0)
        return D3DERR_NOTAVAILABLE


def install(dp):
    if numpy is None:
        return None
    root = Direct3D9Root(dp)
    dp.direct3d9 = root
    hooks = dp.apihooks
    hooks.register_signature("d3d9.dll", "Direct3DCreate9", "i", root.create9)
    hooks.register_signature("d3d9.dll", "Direct3DCreate9Ex", "ip", root.create9ex)
    return root
