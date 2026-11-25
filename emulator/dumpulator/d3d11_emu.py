# Fathom addition to dumpulator: Direct3D 11 (and 11.1) for the emulated program. Boost Software License
# 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# d3d11.dll needs a graphics driver the emulator does not have: D3D11CreateDevice(AndSwapChain) give
# COM objects implemented here (apihooks.ComRuntime). Resources: buffers (their bytes stay in the
# emulator's memory), 1D / 2D / 3D / cube textures with mips and arrays (gpu.Image); views; input
# layouts; vertex, hull, domain, geometry, pixel and compute shaders run by dxbc.py through the pipeline
# of d3d_pipeline.py; constant buffers, samplers, UAVs; the rasterizer / blend / depth-stencil states;
# Draw, DrawIndexed, their instanced and indirect forms, Dispatch(Indirect); clears, copies,
# UpdateSubresource, Map / Unmap, GenerateMips, queries. Swap chains: dxgi_emu.py.

import struct
from typing import Dict, List, Optional

from . import dxbc
from .apihooks import read_astring
from .d3d_pipeline import DrawState, SOTarget, StreamOutput, dispatch, draw_dxbc
from .dxgi_emu import DEVICES, DxgiDevice, Dxgi, dxgi_of, missing, E_INVALIDARG, E_FAIL, DXGI_ERROR_NOT_FOUND, S_FALSE
from .gpu import (DXGI, DXGI_SRGB, Image, Sampler, View, kind_bytes, pitch_of, rows_of, to_srgb)
from .raster import Blend, numpy

E_NOTIMPL = 0x80004001
BIND_RENDER_TARGET, BIND_DEPTH_STENCIL, BIND_UNORDERED_ACCESS = 0x20, 0x40, 0x80
MISC_TEXTURECUBE, MISC_RAW, MISC_STRUCTURED, MISC_GENERATE_MIPS = 0x4, 0x20, 0x40, 0x1


def kind_of(fmt: int, default="rgba8") -> str:
    return DXGI.get(fmt, default)


# ------------------------------------------------------------------ resources
class Resource11:
    """A buffer (bytes in the emulator's memory) or a texture (images[slice][mip])."""

    def __init__(self, d3d, dimension: str, desc: bytes, interface: str):
        self.d3d, self.dimension, self.desc, self.interface = d3d, dimension, bytearray(desc), interface
        self.interfaces = ["IDXGIResource", "IDXGIResource1"]
        self.images: List[List[Image]] = []
        self.address = 0
        self.size = 0
        self.stride = 0
        self.format = 0
        self.mips = 1
        self.misc = 0
        self.bind = 0
        dp = d3d.dp
        if dimension == "buffer":
            self.size, self.usage, self.bind, self.cpu, self.misc, self.stride = struct.unpack("<6I", desc[:24])
            self.address = dp.allocate(max(16, self.size), page_align=True)
            return
        if dimension == "1d":
            width, mips, array, self.format, self.usage, self.bind, self.cpu, self.misc = struct.unpack("<8I", desc[:32])
            height, depth = 1, 1
        elif dimension == "2d":
            (width, height, mips, array, self.format, _, _, self.usage, self.bind, self.cpu,
             self.misc) = struct.unpack("<11I", desc[:44])
            depth = 1
            self.interfaces = ["IDXGISurface", "IDXGISurface1", "IDXGISurface2", "IDXGIResource", "IDXGIResource1"]
        else:
            width, height, depth, mips, self.format, self.usage, self.bind, self.cpu, self.misc = struct.unpack("<9I", desc[:36])
            array = depth
        self.width, self.height, self.depth = max(1, width), max(1, height), max(1, depth)
        self.mips = mips or 1 + max(width, height).bit_length() - 1
        kind = kind_of(self.format)
        slices = max(1, array)
        for _ in range(slices):
            levels = []
            for level in range(self.mips if dimension != "3d" else 1):
                levels.append(Image(max(1, self.width >> level), max(1, self.height >> level), kind))
            self.images.append(levels)

    @property
    def kind(self) -> str:
        return kind_of(self.format)

    # the program's memory ----------------------------------------------------
    def read(self) -> numpy.ndarray:
        return numpy.frombuffer(bytearray(self.d3d.dp.read(self.address, self.size)), dtype=numpy.uint8)

    def write(self, data, offset=0):
        self.d3d.dp.write(self.address + offset, bytes(data))

    def subresource(self, index: int) -> Optional[Image]:
        if not self.images:
            return None
        mips = len(self.images[0])
        slice_index, mip = index // mips, index % mips
        if slice_index >= len(self.images):
            return None
        return self.images[slice_index][mip]

    def initial(self, data_address):
        if not data_address:
            return
        dp = self.d3d.dp
        ptr = dp.ptr_size()
        if self.dimension == "buffer":
            source = dp.read_ptr(data_address)
            if source:
                self.write(bytes(dp.read(source, self.size)))
            return
        stride = 16 if ptr == 8 else 12
        index = 0
        for slice_index, levels in enumerate(self.images):
            for image in levels:
                entry = data_address + stride * index
                source = dp.read_ptr(entry)
                pitch, slice_pitch = struct.unpack("<2I", bytes(dp.read(entry + ptr, 8)))
                if source:
                    rows = rows_of(image.kind, image.height)
                    image.from_bytes(bytes(dp.read(source, max(pitch, image.pitch) * rows)), pitch or image.pitch)
                index += 1
            if self.dimension == "3d":
                # a 3D texture: one entry, its slices one after the other
                entry = data_address
                source = dp.read_ptr(entry)
                pitch, slice_pitch = struct.unpack("<2I", bytes(dp.read(entry + ptr, 8)))
                if source:
                    for z, levels_z in enumerate(self.images):
                        image = levels_z[0]
                        image.from_bytes(bytes(dp.read(source + z * slice_pitch, pitch * image.height)), pitch)
                break

    # COM ---------------------------------------------------------------------
    def GetDesc(self, out):
        self.d3d.dp.write(out, bytes(self.desc))

    def GetType(self, out):
        self.d3d.dp.write(out, struct.pack("<I", {"buffer": 1, "1d": 2, "2d": 3, "3d": 4}[self.dimension]))

    def GetDevice(self, out):
        return self.d3d.device_out(out)

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

    def GetSharedHandle(self, out):
        self.d3d.dp.write_ptr(out, 0)

    def GetUsage(self, out):
        self.d3d.dp.write(out, struct.pack("<I", 0x20))

    def SetEvictionPriority1(self, priority):
        return 0

    # IDXGISurface (a texture asked as a surface)
    def Map(self, locked, flags):
        dp = self.d3d.dp
        image = self.images[0][0]
        dp.write(locked, struct.pack("<I", image.pitch) + b"\0" * (dp.ptr_size() - 4))
        dp.write_ptr(locked + dp.ptr_size(), image.map(dp))

    def Unmap(self):
        self.images[0][0].unmap(self.d3d.dp)

    def GetParent(self, riid, out):
        return self.d3d.device_query(riid, out)


class View11:
    """A view: render target (rtv), depth stencil (dsv), shader resource (srv), unordered access (uav)."""

    def __init__(self, d3d, resource: Resource11, kind: str, desc: Optional[bytes], interface: str):
        self.d3d, self.resource, self.kind, self.desc_bytes, self.interface = d3d, resource, kind, desc, interface
        self.format = resource.format
        self.first_slice, self.slices, self.mip, self.mips = 0, max(1, len(resource.images)), 0, resource.mips
        self.first, self.count, self.flags = 0, 0, 0
        self.dimension = {"buffer": "buffer", "1d": "1d", "2d": "2d", "3d": "3d"}[resource.dimension]
        if resource.dimension == "2d" and resource.misc & MISC_TEXTURECUBE and kind == "srv":
            self.dimension = "cube"
        elif resource.dimension == "2d" and len(resource.images) > 1:
            self.dimension = "2darray"
        elif resource.dimension == "1d" and len(resource.images) > 1:
            self.dimension = "1darray"
        if resource.dimension == "buffer":
            stride = resource.stride if resource.misc & MISC_STRUCTURED else 4
            self.count = resource.size // max(1, stride)
        if desc:
            self._parse(desc)
        self.counter = [0]

    def _parse(self, desc: bytes):
        fmt, dimension = struct.unpack_from("<2I", desc, 0)
        if fmt:
            self.format = fmt
        if self.kind == "dsv":
            values = struct.unpack_from("<3I", desc, 12)
            dims = {1: ("1d", "mip"), 2: ("1darray", "mip_array"), 3: ("2d", "mip"), 4: ("2darray", "mip_array"),
                    5: ("2d", None), 6: ("2darray", "array")}
        elif self.kind == "srv":
            values = struct.unpack_from("<4I", desc, 8)
            dims = {1: ("buffer", "elements"), 2: ("1d", "mips"), 3: ("1darray", "mips_array"), 4: ("2d", "mips"),
                    5: ("2darray", "mips_array"), 6: ("2d", None), 7: ("2darray", "array"), 8: ("3d", "mips"),
                    9: ("cube", "mips"), 10: ("cube", "mips"), 11: ("buffer", "elements_flags")}
        else:
            values = struct.unpack_from("<3I", desc, 8)
            dims = {1: ("buffer", "elements_flags" if self.kind == "uav" else "elements"), 2: ("1d", "mip"),
                    3: ("1darray", "mip_array"), 4: ("2d", "mip"), 5: ("2darray", "mip_array"), 6: ("2d", None),
                    7: ("2darray", "array"), 8: ("3d", "mip_w")}
        name, layout = dims.get(dimension, (self.dimension, None))
        self.dimension = name
        if layout == "elements" or layout == "elements_flags":
            self.first, self.count = values[0], values[1]
            self.flags = values[2] if layout == "elements_flags" else 0
        elif layout == "mips":
            self.mip, self.mips = values[0], values[1] if values[1] != 0xFFFFFFFF else self.resource.mips
        elif layout == "mips_array":
            self.mip, self.mips, self.first_slice, self.slices = values[0], values[1], values[2], values[3]
        elif layout == "mip":
            self.mip = values[0]
        elif layout == "mip_array":
            self.mip, self.first_slice, self.slices = values[0], values[1], values[2]
        elif layout == "array":
            self.first_slice, self.slices = values[0], values[1]
        elif layout == "mip_w":
            self.mip = values[0]

    @property
    def image(self) -> Optional[Image]:
        images = self.resource.images
        if not images:
            return None
        s = min(self.first_slice, len(images) - 1)
        levels = images[s]
        return levels[min(self.mip, len(levels) - 1)]

    def images_of_slices(self):
        images = self.resource.images
        last = min(len(images), self.first_slice + max(1, self.slices))
        return [images[s] for s in range(self.first_slice, last)]

    def gpu_view(self) -> View:
        """The view as the shaders see it (buffers: their bytes read now)."""
        resource = self.resource
        if resource.dimension == "buffer":
            raw = bool(self.flags & 1) or (resource.misc & MISC_RAW and self.format == 0 and not resource.misc & MISC_STRUCTURED)
            structured = resource.misc & MISC_STRUCTURED and not raw
            if raw:
                view = View("raw", count=self.count, first=self.first)
            elif structured:
                view = View("structured", stride=resource.stride, count=self.count, first=self.first)
            else:
                view = View("buffer", fmt=kind_of(self.format, "r32u"), count=self.count, first=self.first)
            view.bytes = resource.read()
            view.counter = self.counter
            view.source = self
            return view
        view = View("texture", images=self.images_of_slices(), dimension=self.dimension, mip=self.mip,
                    mips=max(1, min(self.mips, resource.mips - self.mip)))
        view.srgb = self.format in DXGI_SRGB
        view.source = self
        return view

    def GetResource(self, out):
        self.d3d.dp.write_ptr(out, self.d3d.com.create(self.resource, self.resource.interface))
        self.resource.refs = getattr(self.resource, "refs", 1) + 1

    def GetDesc(self, out):
        if self.desc_bytes:
            self.d3d.dp.write(out, self.desc_bytes)
            return
        size = {"rtv": 20, "dsv": 24, "srv": 24, "uav": 20}[self.kind]
        dimension = {"buffer": 1, "1d": 2, "1darray": 3, "2d": 4, "2darray": 5, "3d": 8, "cube": 9}.get(self.dimension, 4)
        if self.kind == "dsv":
            dimension = {"1d": 1, "1darray": 2, "2d": 3, "2darray": 4}.get(self.dimension, 3)
        data = bytearray(size)
        struct.pack_into("<2I", data, 0, self.format, dimension)
        if self.kind == "srv" and dimension in (2, 4, 8, 9):
            struct.pack_into("<2I", data, 8, self.mip, self.mips)
        self.d3d.dp.write(out, bytes(data))

    GetDesc1 = GetDesc

    def GetDevice(self, out):
        return self.d3d.device_out(out)

    def SetPrivateData(self, guid, size, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND


class State11:
    """Shaders (dxbc.Shader), input layouts, samplers and the fixed states (their description)."""

    def __init__(self, d3d, interface, data=None):
        self.d3d, self.interface, self.data = d3d, interface, data

    def GetDesc(self, out):
        if isinstance(self.data, (bytes, bytearray)):
            self.d3d.dp.write(out, bytes(self.data))

    GetDesc1 = GetDesc

    def GetDevice(self, out):
        return self.d3d.device_out(out)

    def SetPrivateData(self, guid, size, data):
        return 0

    def SetPrivateDataInterface(self, guid, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND

    def GetDataSize(self):
        return 8

    def GetContextFlags(self):
        return 0


def so_entries(dp, address: int, count: int, with_stream: bool):
    """The entries of a D3D11_SO_DECLARATION_ENTRY (with_stream) / D3D10_SO_DECLARATION_ENTRY array: (stream,
    semantic name or None for a gap, semantic index, first component, component count, output slot)."""
    x64 = dp._x64
    ptr = 8 if x64 else 4
    head = (ptr if x64 else 4) if with_stream else 0       # the stream (UINT, padded to the pointer on x64)
    size = head + ptr + 4 + 3
    size = (size + ptr - 1) // ptr * ptr if x64 else (size + 3) // 4 * 4
    out = []
    for i in range(count):
        base = address + size * i
        stream = struct.unpack("<I", bytes(dp.read(base, 4)))[0] if with_stream else 0
        name_address = dp.read_ptr(base + head)
        index = struct.unpack("<I", bytes(dp.read(base + head + ptr, 4)))[0]
        first, components, slot = bytes(dp.read(base + head + ptr + 4, 3))
        name = read_astring(dp, name_address) if name_address else None
        out.append((stream, name, index, first, components, slot))
    return out


_STATE_SKIP = ("d3d", "device", "commands", "mapped", "refs", "com_address")


def _snapshot(context) -> dict:
    return {k: v for k, v in context.__dict__.items() if k not in _STATE_SKIP}


def _copy_state(state: dict) -> dict:
    return {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in state.items()}


def _unmap_copy(target, sub, data: bytes, pitch: int):
    """What a deferred context's Map wrote, into the resource when the command list runs."""
    if target.dimension == "buffer":
        target.write(data[:target.size])
        return
    image = target.subresource(sub)
    if image is not None:
        image.from_bytes(data, pitch)


class CommandList11:
    """The commands of a deferred context (FinishCommandList): (method, state, arguments)."""
    interface = "ID3D11CommandList"

    def __init__(self, d3d, commands):
        self.d3d, self.commands = d3d, commands

    def GetDevice(self, out):
        return self.d3d.device_out(out)

    def GetContextFlags(self):
        return 0

    def SetPrivateData(self, guid, size, data):
        return 0

    def SetPrivateDataInterface(self, guid, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND



def blends_of(state: Optional[State11]) -> List[Blend]:
    """raster.Blend of the 8 render targets of a blend state (D3D11_BLEND_DESC / DESC1)."""
    if state is None or not state.data:
        return [Blend()] * 8
    data = state.data
    independent = struct.unpack_from("<I", data, 4)[0]
    size = 40 if len(data) >= 8 + 40 * 8 else 32
    out = []
    for i in range(8):
        offset = 8 + size * (i if independent else 0)
        if size == 40:
            enable, _, src, dst, op, src_a, dst_a, op_a, _, mask = struct.unpack_from("<9IB", data, offset)
        else:
            enable, src, dst, op, src_a, dst_a, op_a, mask = struct.unpack_from("<7IB", data, offset)
        out.append(Blend(bool(enable), src, dst, op, src_a, dst_a, op_a, mask & 0xF))
    return out


# ------------------------------------------------------------------ the context
class Context11:
    interface = "ID3D11DeviceContext"
    interfaces = ["ID3D11DeviceContext1"]

    def __init__(self, d3d, device):
        self.d3d, self.device = d3d, device
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
        self.constant_buffers: Dict[tuple, tuple] = {}
        self.resources: Dict[tuple, Optional[View11]] = {}
        self.sampler_states: Dict[tuple, Optional[State11]] = {}
        self.uavs: Dict[tuple, Optional[View11]] = {}
        self.rasterizer: Optional[State11] = None
        self.blend: Optional[State11] = None
        self.blend_factor = (1.0, 1.0, 1.0, 1.0)
        self.depth_state: Optional[State11] = None
        self.predicate = None
        self.so_targets: List[tuple] = []      # (Resource11, offset) per slot

    def GetDevice(self, out):
        return self.d3d.device_out(out)

    def GetType(self):
        return 0

    def GetContextFlags(self):
        return 0

    def Flush(self):
        return None

    def SetPrivateData(self, guid, size, data):
        return 0

    def SetPrivateDataInterface(self, guid, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND

    def _list(self, count, array):
        dp = self.d3d.dp
        return [dp.read_ptr(array + dp.ptr_size() * i) if array else 0 for i in range(count)]

    def _impl(self, address):
        return self.d3d.com.impl(address) if address else None

    # stages ------------------------------------------------------------------
    def _set(self, table, stage, start, count, array):
        for i, address in enumerate(self._list(count, array)):
            table[(stage, start + i)] = self._impl(address)

    def _get(self, table, stage, start, count, array):
        dp = self.d3d.dp
        for i in range(count):
            item = table.get((stage, start + i))
            if isinstance(item, tuple):
                item = item[0]
            dp.write_ptr(array + dp.ptr_size() * i, item.com_address if item is not None else 0)
            if item is not None:
                item.refs = getattr(item, "refs", 1) + 1

    def _set_cbs(self, stage, start, count, array, firsts=0, counts=0):
        dp = self.d3d.dp
        for i, address in enumerate(self._list(count, array)):
            first = struct.unpack("<I", bytes(dp.read(firsts + 4 * i, 4)))[0] if firsts else 0
            size = struct.unpack("<I", bytes(dp.read(counts + 4 * i, 4)))[0] if counts else 0
            self.constant_buffers[(stage, start + i)] = (self._impl(address), first, size) if address else (None, 0, 0)

    def _get_shader(self, stage, out, instances, count):
        shader = self.shaders.get(stage)
        self.d3d.dp.write_ptr(out, shader.com_address if shader else 0)
        if count:
            self.d3d.dp.write(count, struct.pack("<I", 0))

    def IASetInputLayout(self, layout):
        self.layout = self._impl(layout)

    def IAGetInputLayout(self, out):
        self.d3d.dp.write_ptr(out, self.layout.com_address if self.layout else 0)

    def IASetVertexBuffers(self, start, count, buffers, strides, offsets):
        dp = self.d3d.dp
        for i, address in enumerate(self._list(count, buffers)):
            stride = struct.unpack("<I", bytes(dp.read(strides + 4 * i, 4)))[0] if strides else 0
            offset = struct.unpack("<I", bytes(dp.read(offsets + 4 * i, 4)))[0] if offsets else 0
            self.vertex_buffers[start + i] = (self._impl(address), stride, offset) if address else None

    def IAGetVertexBuffers(self, start, count, buffers, strides, offsets):
        dp = self.d3d.dp
        for i in range(count):
            entry = self.vertex_buffers.get(start + i)
            if buffers:
                dp.write_ptr(buffers + dp.ptr_size() * i, entry[0].com_address if entry else 0)
            if strides:
                dp.write(strides + 4 * i, struct.pack("<I", entry[1] if entry else 0))
            if offsets:
                dp.write(offsets + 4 * i, struct.pack("<I", entry[2] if entry else 0))

    def IASetIndexBuffer(self, buffer, fmt, offset):
        self.index_buffer = (self._impl(buffer), fmt, offset) if buffer else None

    def IAGetIndexBuffer(self, buffer, fmt, offset):
        dp = self.d3d.dp
        entry = self.index_buffer
        if buffer:
            dp.write_ptr(buffer, entry[0].com_address if entry else 0)
        if fmt:
            dp.write(fmt, struct.pack("<I", entry[1] if entry else 0))
        if offset:
            dp.write(offset, struct.pack("<I", entry[2] if entry else 0))

    def IASetPrimitiveTopology(self, topology):
        self.topology = topology

    def IAGetPrimitiveTopology(self, out):
        self.d3d.dp.write(out, struct.pack("<I", self.topology))

    def OMSetRenderTargets(self, count, views, depth):
        self.targets = [self._impl(a) for a in self._list(count, views)]
        self.depth = self._impl(depth)

    def OMSetRenderTargetsAndUnorderedAccessViews(self, count, views, depth, uav_start, uav_count, uavs, initial):
        if count != 0xFFFFFFFF:
            self.OMSetRenderTargets(count, views, depth)
        if uav_count != 0xFFFFFFFF:
            self._set_uavs("ps", uav_start, uav_count, uavs, initial)

    def _set_uavs(self, stage, start, count, array, initial):
        dp = self.d3d.dp
        for i, address in enumerate(self._list(count, array)):
            view = self._impl(address)
            self.uavs[(stage, start + i)] = view
            if view is not None and initial:
                value = struct.unpack("<I", bytes(dp.read(initial + 4 * i, 4)))[0]
                if value != 0xFFFFFFFF:
                    view.counter[0] = value

    def OMGetRenderTargets(self, count, views, depth):
        dp = self.d3d.dp
        if views:
            for i in range(count):
                view = self.targets[i] if i < len(self.targets) else None
                dp.write_ptr(views + dp.ptr_size() * i, view.com_address if view else 0)
        if depth:
            dp.write_ptr(depth, self.depth.com_address if self.depth else 0)

    def OMGetRenderTargetsAndUnorderedAccessViews(self, count, views, depth, uav_start, uav_count, uavs):
        self.OMGetRenderTargets(count, views, depth)
        if uavs:
            self._get(self.uavs, "ps", uav_start, uav_count, uavs)

    def OMSetBlendState(self, state, factor, mask):
        self.blend = self._impl(state)
        if factor:
            self.blend_factor = struct.unpack("<4f", bytes(self.d3d.dp.read(factor, 16)))
        else:
            self.blend_factor = (1.0, 1.0, 1.0, 1.0)

    def OMGetBlendState(self, state, factor, mask):
        dp = self.d3d.dp
        if state:
            dp.write_ptr(state, self.blend.com_address if self.blend else 0)
        if factor:
            dp.write(factor, struct.pack("<4f", *self.blend_factor))
        if mask:
            dp.write(mask, struct.pack("<I", 0xFFFFFFFF))

    def OMSetDepthStencilState(self, state, reference):
        self.depth_state = self._impl(state)

    def OMGetDepthStencilState(self, state, reference):
        dp = self.d3d.dp
        if state:
            dp.write_ptr(state, self.depth_state.com_address if self.depth_state else 0)
        if reference:
            dp.write(reference, struct.pack("<I", 0))

    def RSSetState(self, state):
        self.rasterizer = self._impl(state)

    def RSGetState(self, out):
        self.d3d.dp.write_ptr(out, self.rasterizer.com_address if self.rasterizer else 0)

    def RSSetViewports(self, count, viewports):
        self.viewports = [struct.unpack("<6f", bytes(self.d3d.dp.read(viewports + 24 * i, 24))) for i in range(count)]

    def RSGetViewports(self, count, viewports):
        dp = self.d3d.dp
        if not viewports:
            dp.write(count, struct.pack("<I", len(self.viewports)))
            return
        n = min(struct.unpack("<I", bytes(dp.read(count, 4)))[0], len(self.viewports))
        for i in range(n):
            dp.write(viewports + 24 * i, struct.pack("<6f", *self.viewports[i]))
        dp.write(count, struct.pack("<I", n))

    def RSSetScissorRects(self, count, rects):
        self.scissors = [struct.unpack("<4i", bytes(self.d3d.dp.read(rects + 16 * i, 16))) for i in range(count)]

    def RSGetScissorRects(self, count, rects):
        dp = self.d3d.dp
        if not rects:
            dp.write(count, struct.pack("<I", len(self.scissors)))
            return
        n = min(struct.unpack("<I", bytes(dp.read(count, 4)))[0], len(self.scissors))
        for i in range(n):
            dp.write(rects + 16 * i, struct.pack("<4i", *self.scissors[i]))
        dp.write(count, struct.pack("<I", n))

    def SOSetTargets(self, count, buffers, offsets):
        dp = self.d3d.dp
        targets = []
        for i, address in enumerate(self._list(count, buffers) if count else []):
            resource = self._impl(address) if address else None
            offset = struct.unpack("<I", bytes(dp.read(offsets + 4 * i, 4)))[0] if offsets else 0
            if resource is not None and offset == 0xFFFFFFFF:     # append after what was written
                offset = getattr(resource, "so_filled", 0)
            targets.append((resource, offset) if isinstance(resource, Resource11) else None)
        self.so_targets = targets

    def SOGetTargets(self, count, buffers):
        dp = self.d3d.dp
        for i in range(count):
            entry = self.so_targets[i] if i < len(self.so_targets) else None
            dp.write_ptr(buffers + dp.ptr_size() * i, entry[0].com_address if entry else 0)

    def _so_counters(self):
        return dict(self.d3d.so_written), dict(self.d3d.so_needed)

    def Begin(self, query):
        impl = self._impl(query)
        if impl is not None:
            impl.begin_fragments = self.d3d.fragments
            impl.begin_so = self._so_counters()

    def End(self, query):
        impl = self._impl(query)
        if impl is not None:
            impl.end_fragments = self.d3d.fragments
            impl.end_so = self._so_counters()

    def GetData(self, query, data, size, flags):
        impl = self._impl(query)
        if not data or not size:
            return 0
        kind = struct.unpack_from("<I", impl.data, 0)[0] if impl is not None and impl.data else 0
        if kind == 1:      # OCCLUSION
            value = struct.pack("<Q", getattr(impl, "end_fragments", 0) - getattr(impl, "begin_fragments", 0))
        elif kind == 5:    # OCCLUSION_PREDICATE
            value = struct.pack("<I", int(getattr(impl, "end_fragments", 0) != getattr(impl, "begin_fragments", 0)))
        elif kind == 2:    # TIMESTAMP
            value = struct.pack("<Q", self.d3d.fragments)
        elif kind == 3:    # TIMESTAMP_DISJOINT
            value = struct.pack("<QI", 1000000000, 0)
        elif kind == 0:    # EVENT
            value = struct.pack("<I", 1)
        elif 6 <= kind <= 15:   # SO_STATISTICS / SO_OVERFLOW_PREDICATE (all streams, then per stream)
            begin = getattr(impl, "begin_so", ({}, {}))
            end = getattr(impl, "end_so", begin)
            streams = range(4) if kind in (6, 7) else [(kind - 8) // 2]
            written = sum(end[0].get(s, 0) - begin[0].get(s, 0) for s in streams)
            needed = sum(end[1].get(s, 0) - begin[1].get(s, 0) for s in streams)
            value = struct.pack("<QQ", written, needed) if kind in (6, 8, 10, 12, 14) else struct.pack("<I", int(needed > written))
        else:
            value = b""
        self.d3d.dp.write(data, value.ljust(size, b"\0")[:size])
        return 0

    def SetPredication(self, predicate, value):
        self.predicate = (self._impl(predicate), value) if predicate else None

    def GetPredication(self, predicate, value):
        dp = self.d3d.dp
        if predicate:
            dp.write_ptr(predicate, 0)
        if value:
            dp.write(value, struct.pack("<I", 0))

    def SetResourceMinLOD(self, resource, lod):
        return None

    def GetResourceMinLOD(self, resource):
        return 0

    def ExecuteCommandList(self, command_list, restore):
        """The commands a deferred context recorded, each with the state it had then; afterwards the context's
        own state is kept (restore) or cleared."""
        commands = self._impl(command_list)
        if not isinstance(commands, CommandList11):
            return
        saved = _snapshot(self) if restore else None
        for name, state, args in commands.commands:
            if name == "unmap":
                _unmap_copy(*args)
                continue
            context = Context11.__new__(Context11)
            context.d3d, context.device = self.d3d, self.device
            context.__dict__.update(_copy_state(state))
            getattr(Context11, name)(context, *args)
        if restore:
            self.__dict__.update(_copy_state(saved))
        else:
            self.ClearState()

    def FinishCommandList(self, restore, out):
        return E_FAIL

    # clears, copies, maps ------------------------------------------------------
    def ClearRenderTargetView(self, view, color):
        impl = self._impl(view)
        if not isinstance(impl, View11):
            return
        rgba = struct.unpack("<4f", bytes(self.d3d.dp.read(color, 16)))
        if impl.format in DXGI_SRGB:
            rgba = tuple(to_srgb(numpy.array([rgba], dtype=numpy.float32))[0])
        for levels in impl.images_of_slices():
            image = levels[min(impl.mip, len(levels) - 1)]
            image.clear(rgba)

    def ClearDepthStencilView(self, view, flags, depth, stencil):
        impl = self._impl(view)
        if isinstance(impl, View11) and flags & 1:
            for levels in impl.images_of_slices():
                image = levels[min(impl.mip, len(levels) - 1)]
                image.fb.depth[:] = depth
                image.changed()

    def _clear_uav(self, view, values, integer):
        impl = self._impl(view)
        if not isinstance(impl, View11):
            return
        if impl.resource.dimension == "buffer":
            gpu_view = impl.gpu_view()
            if gpu_view.kind in ("raw", "structured"):
                data = numpy.full(len(gpu_view.bytes) // 4, values[0], dtype=numpy.uint32)
                impl.resource.write(data.tobytes())
            return
        for levels in impl.images_of_slices():
            image = levels[min(impl.mip, len(levels) - 1)]
            image.set_texels(numpy.broadcast_to(numpy.array(values, dtype=numpy.float32), image.texels().shape).copy())

    def ClearUnorderedAccessViewFloat(self, view, values):
        self._clear_uav(view, struct.unpack("<4f", bytes(self.d3d.dp.read(values, 16))), False)

    def ClearUnorderedAccessViewUint(self, view, values):
        self._clear_uav(view, struct.unpack("<4I", bytes(self.d3d.dp.read(values, 16))), True)

    def ClearView(self, view, color, rects, count):
        impl = self._impl(view)
        rgba = struct.unpack("<4f", bytes(self.d3d.dp.read(color, 16)))
        if isinstance(impl, View11) and impl.kind == "rtv":
            impl.image.clear(rgba)

    def DiscardResource(self, resource):
        return None

    def DiscardView(self, view):
        return None

    def DiscardView1(self, view, rects, count):
        return None

    def CopyResource(self, dst, src):
        target, source = self._impl(dst), self._impl(src)
        if not isinstance(target, Resource11) or not isinstance(source, Resource11):
            return
        if target.dimension == "buffer" and source.dimension == "buffer":
            target.write(source.read()[:target.size])
            return
        for a_levels, b_levels in zip(target.images, source.images):
            for a, b in zip(a_levels, b_levels):
                a.copy_from(b)

    def CopySubresourceRegion(self, dst, dst_sub, x, y, z, src, src_sub, box):
        target, source = self._impl(dst), self._impl(src)
        if not isinstance(target, Resource11) or not isinstance(source, Resource11):
            return
        dp = self.d3d.dp
        area = struct.unpack("<6I", bytes(dp.read(box, 24))) if box else None
        if target.dimension == "buffer":
            begin, end = (area[0], area[3]) if area else (0, source.size)
            target.write(source.read()[begin:end], x)
            return
        a, b = target.subresource(dst_sub), source.subresource(src_sub)
        if a is not None and b is not None:
            a.copy_from(b, (area[0], area[1], area[3], area[4]) if area else None, (x, y))

    def CopySubresourceRegion1(self, dst, dst_sub, x, y, z, src, src_sub, box, flags):
        return self.CopySubresourceRegion(dst, dst_sub, x, y, z, src, src_sub, box)

    def UpdateSubresource(self, dst, sub, box, data, row_pitch, depth_pitch):
        target = self._impl(dst)
        if not isinstance(target, Resource11) or not data:
            return
        dp = self.d3d.dp
        area = struct.unpack("<6I", bytes(dp.read(box, 24))) if box else None
        if target.dimension == "buffer":
            begin, end = (area[0], area[3]) if area else (0, target.size)
            target.write(bytes(dp.read(data, end - begin)), begin)
            return
        image = target.subresource(sub)
        if image is None:
            return
        rect = (area[0], area[1], area[3], area[4]) if area else (0, 0, image.width, image.height)
        rows = rows_of(image.kind, rect[3] - rect[1])
        image.from_bytes(bytes(dp.read(data, max(row_pitch, 1) * rows)), row_pitch or None, rect if area else None)

    def UpdateSubresource1(self, dst, sub, box, data, row_pitch, depth_pitch, flags):
        return self.UpdateSubresource(dst, sub, box, data, row_pitch, depth_pitch)

    def Map(self, resource, sub, kind, flags, mapped):
        target = self._impl(resource)
        if not isinstance(target, Resource11):
            return E_INVALIDARG
        dp = self.d3d.dp
        if target.dimension == "buffer":
            address, pitch, depth_pitch = target.address, target.size, target.size
        else:
            image = target.subresource(sub)
            if image is None:
                return E_INVALIDARG
            address, pitch = image.map(dp), image.pitch
            depth_pitch = pitch * rows_of(image.kind, image.height)
        if mapped:
            dp.write_ptr(mapped, address)
            dp.write(mapped + dp.ptr_size(), struct.pack("<2I", pitch, depth_pitch))
        return 0

    def Unmap(self, resource, sub):
        target = self._impl(resource)
        if isinstance(target, Resource11) and target.dimension != "buffer":
            image = target.subresource(sub)
            if image is not None:
                image.unmap(self.d3d.dp)

    def GenerateMips(self, view):
        impl = self._impl(view)
        if not isinstance(impl, View11):
            return
        for levels in impl.resource.images:
            for level in range(1, len(levels)):
                src = levels[level - 1].texels()
                h, w = levels[level].height, levels[level].width
                trimmed = src[:h * 2, :w * 2] if src.shape[0] >= 2 and src.shape[1] >= 2 else src
                if trimmed.shape[0] >= 2 * h and trimmed.shape[1] >= 2 * w:
                    down = trimmed.reshape(h, 2, w, 2, 4).mean(axis=(1, 3))
                else:
                    down = numpy.resize(trimmed, (h, w, 4))
                levels[level].set_texels(down.astype(numpy.float32))

    def ResolveSubresource(self, dst, dst_sub, src, src_sub, fmt):
        target, source = self._impl(dst), self._impl(src)
        if isinstance(target, Resource11) and isinstance(source, Resource11):
            a, b = target.subresource(dst_sub), source.subresource(src_sub)
            if a is not None and b is not None:
                a.copy_from(b)

    def CopyStructureCount(self, dst, offset, view):
        target, impl = self._impl(dst), self._impl(view)
        if isinstance(target, Resource11) and isinstance(impl, View11):
            target.write(struct.pack("<I", impl.counter[0] & 0xFFFFFFFF), offset)

    # shaders and their resources ----------------------------------------------
    def _shader(self, stage, shader):
        self.shaders[stage] = self._impl(shader)

    def VSSetShader(self, shader, instances, count):
        self._shader("vs", shader)

    def PSSetShader(self, shader, instances, count):
        self._shader("ps", shader)

    def GSSetShader(self, shader, instances, count):
        self._shader("gs", shader)

    def HSSetShader(self, shader, instances, count):
        self._shader("hs", shader)

    def DSSetShader(self, shader, instances, count):
        self._shader("ds", shader)

    def CSSetShader(self, shader, instances, count):
        self._shader("cs", shader)

    def CSSetUnorderedAccessViews(self, start, count, uavs, initial):
        self._set_uavs("cs", start, count, uavs, initial)

    def CSGetUnorderedAccessViews(self, start, count, uavs):
        self._get(self.uavs, "cs", start, count, uavs)

    # bindings for a draw ------------------------------------------------------
    def _bindings(self, stage, views_out):
        constant_buffers = {}
        for (s, slot), (buffer, first, count) in self.constant_buffers.items():
            if s == stage and buffer is not None:
                data = buffer.read()
                if count:
                    data = data[first * 16:(first + count) * 16]
                data = numpy.concatenate([data, numpy.zeros(-len(data) % 16, dtype=numpy.uint8)])
                constant_buffers[slot] = data.view("<u4").reshape(-1, 4)
        srvs = {slot: view.gpu_view() for (s, slot), view in self.resources.items() if s == stage and view is not None}
        samplers = {slot: Sampler.from_d3d11(state.data) for (s, slot), state in self.sampler_states.items()
                    if s == stage and state is not None and state.data}
        uavs = {}
        if stage in ("cs", "ps"):
            for (s, slot), view in self.uavs.items():
                if s == stage and view is not None:
                    uavs[slot] = view.gpu_view()
                    views_out.append(uavs[slot])
        return dxbc.Bindings(constant_buffers, srvs, samplers, uavs)

    @staticmethod
    def _write_back(views):
        for view in views:
            source = getattr(view, "source", None)
            if view.bytes is not None and view.dirty and source is not None:
                source.resource.write(view.bytes)

    def _shader_of(self, stage):
        state = self.shaders.get(stage)
        if state is None:
            return None, True
        if state.data is None:
            missing(self.d3d.dp, f"Direct3D 11 shader ({stage}): {getattr(state, 'error', 'not usable')}")
            return None, False
        return state.data, True

    def _state(self) -> Optional[DrawState]:
        state = DrawState()
        for stage in ("vs", "hs", "ds", "gs", "ps"):
            shader, ok = self._shader_of(stage)
            if not ok:
                return None
            if shader is not None:
                if shader.error:
                    missing(self.d3d.dp, f"Direct3D 11 shader: {shader.error}")
                    return None
                state.shaders[stage] = shader
        if "vs" not in state.shaders:
            missing(self.d3d.dp, "Direct3D 11 draw without a vertex shader")
            return None
        gs_state = self.shaders.get("gs")
        so = getattr(gs_state, "so", None) if gs_state is not None else None
        if so is not None:
            entries, strides, rasterized = so
            state.stream_out = StreamOutput(entries, strides, rasterized)
            for slot, entry in enumerate(self.so_targets[:4]):
                if entry is not None:
                    resource, offset = entry
                    state.stream_out.targets[slot] = SOTarget(resource.read(), offset, resource.size, resource)
            if state.shaders.get("gs") is not None and state.shaders["gs"].kind != 2:
                del state.shaders["gs"]     # a vertex / domain shader given for stream output: no geometry shader
        state.layout = self.layout.data if self.layout is not None and self.layout.data else []
        for slot, entry in self.vertex_buffers.items():
            if entry and entry[0] is not None:
                buffer, stride, offset = entry
                state.vertex_buffers[slot] = (buffer.read(), stride, offset)
        state.topology = self.topology
        state.viewports = list(self.viewports)
        state.scissors = list(self.scissors)
        state.targets = [view.image if view is not None else None for view in self.targets]
        state.srgb = {i for i, view in enumerate(self.targets) if view is not None and view.format in DXGI_SRGB}
        state.depth = self.depth.image if self.depth is not None else None
        state.blends = blends_of(self.blend)
        state.blend_factor = self.blend_factor
        if self.depth_state is not None and self.depth_state.data:
            enabled, write, function = struct.unpack_from("<3I", self.depth_state.data, 0)
            state.depth_state = (bool(enabled), bool(write), function)
        if self.rasterizer is not None and self.rasterizer.data:
            fill, cull, front_ccw = struct.unpack_from("<3I", self.rasterizer.data, 0)
            depth_clip, scissor = struct.unpack_from("<2I", self.rasterizer.data, 24)
            state.rasterizer = (fill, cull, bool(front_ccw), bool(depth_clip), bool(scissor))
        return state

    def _draw(self, vertex_ids, instances):
        state = self._state()
        if state is None or not len(vertex_ids):
            return
        if self.predicate is not None:
            query, value = self.predicate
            passed = getattr(query, "end_fragments", 0) != getattr(query, "begin_fragments", 0)
            if passed == bool(value):
                return
        uav_views: List[View] = []
        for stage in ("vs", "hs", "ds", "gs", "ps"):
            if stage in state.shaders:
                state.bindings[stage] = self._bindings(stage, uav_views)
        try:
            self.d3d.fragments += draw_dxbc(state, vertex_ids, instances)
        except dxbc.Unsupported as e:
            missing(self.d3d.dp, f"Direct3D 11 shader: {e}")
        self._write_back(uav_views)
        self._stream_out_done(state)

    def _stream_out_done(self, state):
        """The stream output's buffers back in the program's memory, their filled size (DrawAuto), the
        statistics."""
        so = state.stream_out
        if so is None:
            return
        for slot, target in enumerate(so.targets):
            if target is None:
                continue
            if target.changed:
                target.owner.write(target.data[:target.owner.size])
            target.owner.so_filled = target.offset
            if slot < len(self.so_targets) and self.so_targets[slot] is not None:
                self.so_targets[slot] = (target.owner, target.offset)
        for stream, count in so.written.items():
            self.d3d.so_written[stream] = self.d3d.so_written.get(stream, 0) + count
        for stream, count in so.needed.items():
            self.d3d.so_needed[stream] = self.d3d.so_needed.get(stream, 0) + count

    def Draw(self, count, start):
        self._draw(numpy.arange(start, start + count), [(0, 0)])

    def DrawInstanced(self, count, instances, start, start_instance):
        self._draw(numpy.arange(start, start + count), [(i, start_instance + i) for i in range(instances)])

    def _indices(self, count, start, base):
        if self.index_buffer is None or self.index_buffer[0] is None:
            return numpy.zeros(0, dtype=numpy.int64)
        buffer, fmt, offset = self.index_buffer
        size = 4 if fmt == 42 else 2
        data = buffer.read()
        at = offset + start * size
        count = max(0, min(count, (len(data) - at) // size))
        values = numpy.frombuffer(data[at:at + count * size].tobytes(), dtype="<u4" if size == 4 else "<u2").astype(numpy.int64)
        base = base - (1 << 32) if base & 0x80000000 else base
        return values + base

    def DrawIndexed(self, count, start, base):
        self._draw(self._indices(count, start, base), [(0, 0)])

    def DrawIndexedInstanced(self, count, instances, start, base, start_instance):
        self._draw(self._indices(count, start, base), [(i, start_instance + i) for i in range(instances)])

    def _arguments(self, buffer, offset, count):
        impl = self._impl(buffer)
        if not isinstance(impl, Resource11):
            return None
        return struct.unpack_from(f"<{count}I", impl.read().tobytes(), offset)

    def DrawInstancedIndirect(self, buffer, offset):
        args = self._arguments(buffer, offset, 4)
        if args:
            self.DrawInstanced(*args)

    def DrawIndexedInstancedIndirect(self, buffer, offset):
        args = self._arguments(buffer, offset, 5)
        if args:
            self.DrawIndexedInstanced(*args)

    def DrawAuto(self):
        """A draw of the vertices the stream output wrote to the buffer of input slot 0."""
        entry = self.vertex_buffers.get(0)
        if not entry or entry[0] is None or not entry[1]:
            return
        buffer, stride, offset = entry
        count = max(0, getattr(buffer, "so_filled", 0) - offset) // stride
        self.Draw(count, 0)

    def Dispatch(self, x, y, z):
        shader, ok = self._shader_of("cs")
        if shader is None:
            return
        if shader.error:
            missing(self.d3d.dp, f"Direct3D 11 compute shader: {shader.error}")
            return
        uav_views: List[View] = []
        bindings = self._bindings("cs", uav_views)
        try:
            dispatch(shader, (x, y, z), bindings)
        except dxbc.Unsupported as e:
            missing(self.d3d.dp, f"Direct3D 11 compute shader: {e}")
        self._write_back(uav_views)

    def DispatchIndirect(self, buffer, offset):
        args = self._arguments(buffer, offset, 3)
        if args:
            self.Dispatch(*args)

    def SwapDeviceContextState(self, state, previous):
        if previous:
            self.d3d.dp.write_ptr(previous, 0)


def _stage_methods():
    """XXSetConstantBuffers(1) / XXSetShaderResources / XXSetSamplers and their Get of each stage."""
    for prefix in ("VS", "PS", "GS", "HS", "DS", "CS"):
        stage = prefix.lower()
        setattr(Context11, f"{prefix}SetConstantBuffers",
                (lambda s: lambda self, start, count, array: self._set_cbs(s, start, count, array))(stage))
        setattr(Context11, f"{prefix}SetConstantBuffers1",
                (lambda s: lambda self, start, count, array, firsts, counts: self._set_cbs(s, start, count, array, firsts, counts))(stage))
        setattr(Context11, f"{prefix}GetConstantBuffers",
                (lambda s: lambda self, start, count, array: self._get(self.constant_buffers, s, start, count, array))(stage))
        setattr(Context11, f"{prefix}GetConstantBuffers1",
                (lambda s: lambda self, start, count, array, firsts, counts: self._get(self.constant_buffers, s, start, count, array))(stage))
        setattr(Context11, f"{prefix}SetShaderResources",
                (lambda s: lambda self, start, count, array: self._set(self.resources, s, start, count, array))(stage))
        setattr(Context11, f"{prefix}GetShaderResources",
                (lambda s: lambda self, start, count, array: self._get(self.resources, s, start, count, array))(stage))
        setattr(Context11, f"{prefix}SetSamplers",
                (lambda s: lambda self, start, count, array: self._set(self.sampler_states, s, start, count, array))(stage))
        setattr(Context11, f"{prefix}GetSamplers",
                (lambda s: lambda self, start, count, array: self._get(self.sampler_states, s, start, count, array))(stage))
        setattr(Context11, f"{prefix}GetShader",
                (lambda s: lambda self, out, instances, count: self._get_shader(s, out, instances, count))(stage))


_stage_methods()


# ------------------------------------------------------------------ the device
class Device11(DxgiDevice):
    interface = "ID3D11Device"
    interfaces = ["ID3D11Device1"] + DEVICES

    def __init__(self, d3d, level, flags):
        self.d3d, self.dxgi, self.level, self.flags = d3d, d3d.dxgi, level, flags
        self.context = Context11(d3d, self)

    def swapchain_buffer(self, width, height, fmt):
        desc = struct.pack("<11I", width, height, 1, 1, fmt, 1, 0, 0, BIND_RENDER_TARGET, 0, 0)
        return Resource11(self.d3d, "2d", desc, "ID3D11Texture2D")

    def _out(self, impl, interface, out):
        return self.dxgi.out(impl, interface, out)

    def CreateBuffer(self, desc, initial, out):
        resource = Resource11(self.d3d, "buffer", bytes(self.d3d.dp.read(desc, 24)), "ID3D11Buffer")
        resource.initial(initial)
        return self._out(resource, "ID3D11Buffer", out)

    def CreateTexture1D(self, desc, initial, out):
        resource = Resource11(self.d3d, "1d", bytes(self.d3d.dp.read(desc, 32)), "ID3D11Texture1D")
        resource.initial(initial)
        return self._out(resource, "ID3D11Texture1D", out)

    def CreateTexture2D(self, desc, initial, out):
        resource = Resource11(self.d3d, "2d", bytes(self.d3d.dp.read(desc, 44)), "ID3D11Texture2D")
        resource.initial(initial)
        return self._out(resource, "ID3D11Texture2D", out)

    def CreateTexture3D(self, desc, initial, out):
        resource = Resource11(self.d3d, "3d", bytes(self.d3d.dp.read(desc, 36)), "ID3D11Texture3D")
        resource.initial(initial)
        return self._out(resource, "ID3D11Texture3D", out)

    def _view(self, resource, desc, out, kind, interface, size):
        impl = self.d3d.com.impl(resource)
        if not isinstance(impl, Resource11):
            return E_INVALIDARG
        view = View11(self.d3d, impl, kind, bytes(self.d3d.dp.read(desc, size)) if desc else None, interface)
        return self._out(view, interface, out)

    def CreateRenderTargetView(self, resource, desc, out):
        return self._view(resource, desc, out, "rtv", "ID3D11RenderTargetView", 20)

    def CreateDepthStencilView(self, resource, desc, out):
        return self._view(resource, desc, out, "dsv", "ID3D11DepthStencilView", 24)

    def CreateShaderResourceView(self, resource, desc, out):
        return self._view(resource, desc, out, "srv", "ID3D11ShaderResourceView", 24)

    def CreateUnorderedAccessView(self, resource, desc, out):
        return self._view(resource, desc, out, "uav", "ID3D11UnorderedAccessView", 20)

    def CreateInputLayout(self, descs, count, bytecode, length, out):
        return self._out(State11(self.d3d, "ID3D11InputLayout", input_layout(self.d3d.dp, descs, count)),
                         "ID3D11InputLayout", out)

    def _shader(self, bytecode, length, out, interface):
        state = State11(self.d3d, interface, None)
        try:
            state.data = dxbc.Shader(bytes(self.d3d.dp.read(bytecode, length)))
        except (dxbc.Unsupported, struct.error, ValueError) as e:
            state.error = str(e)
            missing(self.d3d.dp, f"Direct3D 11 shader: {e}")
        return self._out(state, interface, out)

    def CreateVertexShader(self, bytecode, length, linkage, out):
        return self._shader(bytecode, length, out, "ID3D11VertexShader")

    def CreatePixelShader(self, bytecode, length, linkage, out):
        return self._shader(bytecode, length, out, "ID3D11PixelShader")

    def CreateGeometryShader(self, bytecode, length, linkage, out):
        return self._shader(bytecode, length, out, "ID3D11GeometryShader")

    def CreateGeometryShaderWithStreamOutput(self, bytecode, length, decl, entries, strides, count, stream, linkage, out):
        result = self._shader(bytecode, length, out, "ID3D11GeometryShader")
        state = self.d3d.com.impl(self.d3d.dp.read_ptr(out)) if out and result == 0 else None
        if state is not None:
            dp = self.d3d.dp
            stride_list = [struct.unpack("<I", bytes(dp.read(strides + 4 * i, 4)))[0] for i in range(count)] if strides else []
            state.so = (so_entries(dp, decl, entries, True), stride_list, -1 if stream == 0xFFFFFFFF else stream)
        return result

    def CreateHullShader(self, bytecode, length, linkage, out):
        return self._shader(bytecode, length, out, "ID3D11HullShader")

    def CreateDomainShader(self, bytecode, length, linkage, out):
        return self._shader(bytecode, length, out, "ID3D11DomainShader")

    def CreateComputeShader(self, bytecode, length, linkage, out):
        return self._shader(bytecode, length, out, "ID3D11ComputeShader")

    def CreateClassLinkage(self, out):
        return self._out(State11(self.d3d, "ID3D11ClassLinkage"), "ID3D11ClassLinkage", out)

    def _state(self, desc, size, out, interface):
        return self._out(State11(self.d3d, interface, bytes(self.d3d.dp.read(desc, size))), interface, out)

    def CreateBlendState(self, desc, out):
        return self._state(desc, 8 + 32 * 8, out, "ID3D11BlendState")

    def CreateBlendState1(self, desc, out):
        return self._state(desc, 8 + 40 * 8, out, "ID3D11BlendState1")

    def CreateDepthStencilState(self, desc, out):
        return self._state(desc, 52, out, "ID3D11DepthStencilState")

    def CreateRasterizerState(self, desc, out):
        return self._state(desc, 40, out, "ID3D11RasterizerState")

    def CreateRasterizerState1(self, desc, out):
        return self._state(desc, 44, out, "ID3D11RasterizerState1")

    def CreateSamplerState(self, desc, out):
        return self._state(desc, 52, out, "ID3D11SamplerState")

    def CreateQuery(self, desc, out):
        return self._state(desc, 8, out, "ID3D11Query")

    def CreatePredicate(self, desc, out):
        return self._state(desc, 8, out, "ID3D11Predicate")

    def CreateCounter(self, desc, out):
        return E_INVALIDARG

    def CreateDeferredContext(self, flags, out):
        context = DeferredContext11(self.d3d, self)
        self.d3d.dp.write_ptr(out, self.d3d.com.create(context, "ID3D11DeviceContext"))
        return 0

    CreateDeferredContext1 = CreateDeferredContext
    CreateDeferredContext2 = CreateDeferredContext
    CreateDeferredContext3 = CreateDeferredContext

    def CreateDeviceContextState(self, flags, levels, count, sdk, riid, out, chosen):
        if chosen:
            self.d3d.dp.write(chosen, struct.pack("<I", self.level))
        return self._out(State11(self.d3d, "ID3D11DeviceChild"), "ID3D11BlendState", out) if out else 0

    def OpenSharedResource(self, handle, riid, out):
        return E_INVALIDARG

    def CheckFormatSupport(self, fmt, out):
        self.d3d.dp.write(out, struct.pack("<I", 0x3FFFFFFF))

    def CheckMultisampleQualityLevels(self, fmt, count, out):
        self.d3d.dp.write(out, struct.pack("<I", 1 if count == 1 else 0))

    def CheckFeatureSupport(self, feature, data, size):
        self.d3d.dp.write(data, b"\0" * size)
        if feature == 2 and size >= 4:   # D3D11_FEATURE_FORMAT_SUPPORT: the format, then its support
            self.d3d.dp.write(data + 4, struct.pack("<I", 0x3FFFFFFF))

    def CheckCounterInfo(self, info):
        self.d3d.dp.write(info, b"\0" * 12)

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
        self.d3d.dp.write_ptr(out, self.d3d.com.create(self.context, "ID3D11DeviceContext"))
        self.context.refs = getattr(self.context, "refs", 1) + 1

    def GetImmediateContext1(self, out):
        self.d3d.dp.write_ptr(out, self.d3d.com.create(self.context, "ID3D11DeviceContext1"))

    def SetExceptionMode(self, flags):
        return 0

    def GetExceptionMode(self):
        return 0


def input_layout(dp, descs, count):
    """D3D11 / D3D10 / D3D12 input element descriptions (the same layout): dicts for d3d_pipeline."""
    from .apihooks import read_astring
    ptr = dp.ptr_size()
    size = 32 if ptr == 8 else 28
    elements, offsets = [], {}
    for i in range(count):
        base = descs + size * i
        name = dp.read_ptr(base)
        index, fmt, slot, offset, classification, step = struct.unpack("<6I", bytes(dp.read(base + ptr, 24)))
        kind = DXGI.get(fmt, "rgba32f")
        if offset == 0xFFFFFFFF:
            offset = offsets.get(slot, 0)
        offsets[slot] = offset + kind_bytes(kind)
        elements.append(dict(name=(read_astring(dp, name) or "").upper(), index=index, kind=kind, slot=slot,
                             offset=offset, instanced=classification == 1, step=step))
    return elements


class Direct3D11:
    def __init__(self, dp):
        self.dp = dp
        self.dxgi: Dxgi = dxgi_of(dp)
        self.com = self.dxgi.com
        self.devices: List[Device11] = []
        self.fragments = 0
        self.so_written: Dict[int, int] = {}    # stream output: primitives written / needed per stream
        self.so_needed: Dict[int, int] = {}

    def device_out(self, out):
        if not self.devices:
            return E_FAIL
        self.dp.write_ptr(out, self.com.create(self.devices[-1], "ID3D11Device"))
        return 0

    def device_query(self, riid, out):
        return self.dxgi.query(self.devices[-1], riid, out) if self.devices else E_FAIL

    def create(self, dp, adapter, driver, software, flags, levels, count, sdk, device_out, level_out, context_out,
               chain_desc=0, chain_out=0):
        level = 0xB100
        if levels and count:
            requested = struct.unpack(f"<{count}I", bytes(dp.read(levels, 4 * count)))
            supported = [lvl for lvl in requested if lvl <= 0xB100]
            if not supported:
                return E_INVALIDARG
            level = supported[0]
        device = Device11(self, level, flags)
        self.devices.append(device)
        if level_out:
            dp.write(level_out, struct.pack("<I", level))
        if device_out:
            dp.write_ptr(device_out, self.com.create(device, "ID3D11Device"))
        if context_out:
            dp.write_ptr(context_out, self.com.create(device.context, "ID3D11DeviceContext"))
        if chain_desc and chain_out:
            result = self.dxgi.factory.CreateSwapChain(self.com.create(device, "ID3D11Device"), chain_desc, chain_out)
            if result:
                return result
        self.dxgi.events.append(dict(kind="direct3d11_device", level=hex(level)))
        return 0 if device_out or context_out or chain_out else S_FALSE

    def create_with_chain(self, dp, adapter, driver, software, flags, levels, count, sdk, chain_desc, chain_out,
                          device_out, level_out, context_out):
        return self.create(dp, adapter, driver, software, flags, levels, count, sdk, device_out, level_out, context_out,
                           chain_desc, chain_out)


def install(dp):
    if numpy is None:
        return None
    d3d = Direct3D11(dp)
    dp.direct3d11 = d3d
    hooks = dp.apihooks
    hooks.register_signature("d3d11.dll", "D3D11CreateDevice", "pipipiippp", d3d.create)
    hooks.register_signature("d3d11.dll", "D3D11CreateDeviceAndSwapChain", "pipipiippppp", d3d.create_with_chain)
    return d3d


class DeferredContext11(Context11):
    """A deferred context: the state is its own; the commands that act (draws, dispatches, clears, copies,
    updates, maps, queries) are recorded with the state of the moment and copies of the data they point to,
    for ExecuteCommandList on the immediate context."""

    def __init__(self, d3d, device):
        self.commands: List[tuple] = []
        self.mapped: Dict[tuple, tuple] = {}
        Context11.__init__(self, d3d, device)

    def GetType(self):
        return 1    # D3D11_DEVICE_CONTEXT_DEFERRED

    def _copy(self, address: int, size: int) -> int:
        """A copy of size bytes of the program's memory, kept for the command list."""
        if not address or size <= 0:
            return address
        dp = self.d3d.dp
        copy = dp.allocate(max(16, size))
        dp.write(copy, bytes(dp.read(address, size)))
        return copy

    def _record(self, name, *args):
        self.commands.append((name, _copy_state(_snapshot(self)), args))

    def FinishCommandList(self, restore, out):
        commands = CommandList11(self.d3d, self.commands)
        self.commands = []
        if not restore:
            self.ClearState()
        if out:
            self.d3d.dp.write_ptr(out, self.d3d.com.create(commands, "ID3D11CommandList"))
        return 0

    def ExecuteCommandList(self, command_list, restore):
        self._record("ExecuteCommandList", command_list, restore)

    # the commands -----------------------------------------------------------
    def ClearRenderTargetView(self, view, color):
        self._record("ClearRenderTargetView", view, self._copy(color, 16))

    def ClearUnorderedAccessViewFloat(self, view, values):
        self._record("ClearUnorderedAccessViewFloat", view, self._copy(values, 16))

    def ClearUnorderedAccessViewUint(self, view, values):
        self._record("ClearUnorderedAccessViewUint", view, self._copy(values, 16))

    def ClearView(self, view, color, rects, count):
        self._record("ClearView", view, self._copy(color, 16), self._copy(rects, 16 * count), count)

    def CopySubresourceRegion(self, dst, dst_sub, x, y, z, src, src_sub, box):
        self._record("CopySubresourceRegion", dst, dst_sub, x, y, z, src, src_sub, self._copy(box, 24))

    def CopySubresourceRegion1(self, dst, dst_sub, x, y, z, src, src_sub, box, flags):
        self._record("CopySubresourceRegion", dst, dst_sub, x, y, z, src, src_sub, self._copy(box, 24))

    def UpdateSubresource(self, dst, sub, box, data, row_pitch, depth_pitch):
        target = self._impl(dst)
        if not isinstance(target, Resource11) or not data:
            return
        dp = self.d3d.dp
        area = struct.unpack("<6I", bytes(dp.read(box, 24))) if box else None
        if target.dimension == "buffer":
            size = (area[3] - area[0]) if area else target.size
        else:
            image = target.subresource(sub)
            if image is None:
                return
            height = (area[4] - area[1]) if area else image.height
            size = max(row_pitch, 1) * rows_of(image.kind, height)
        self._record("UpdateSubresource", dst, sub, self._copy(box, 24), self._copy(data, size), row_pitch, depth_pitch)

    def UpdateSubresource1(self, dst, sub, box, data, row_pitch, depth_pitch, flags):
        return self.UpdateSubresource(dst, sub, box, data, row_pitch, depth_pitch)

    def Map(self, resource, sub, kind, flags, mapped):
        """Only WRITE_DISCARD / WRITE_NO_OVERWRITE on a deferred context: fresh memory, copied at Unmap."""
        target = self._impl(resource)
        if not isinstance(target, Resource11):
            return E_INVALIDARG
        dp = self.d3d.dp
        if target.dimension == "buffer":
            size, pitch, depth_pitch = target.size, target.size, target.size
        else:
            image = target.subresource(sub)
            if image is None:
                return E_INVALIDARG
            pitch = image.pitch
            size = depth_pitch = pitch * rows_of(image.kind, image.height)
        address = dp.allocate(max(16, size), page_align=True)
        if kind == 5 and target.dimension == "buffer":     # NO_OVERWRITE: what is there stays
            dp.write(address, bytes(target.read()))
        self.mapped[(id(target), sub)] = (address, size, pitch)
        if mapped:
            dp.write_ptr(mapped, address)
            dp.write(mapped + dp.ptr_size(), struct.pack("<2I", pitch, depth_pitch))
        return 0

    def Unmap(self, resource, sub):
        target = self._impl(resource)
        entry = self.mapped.pop((id(target), sub), None) if target is not None else None
        if entry is None:
            return
        address, size, pitch = entry
        self.commands.append(("unmap", None, (target, sub, bytes(self.d3d.dp.read(address, size)), pitch)))


for _name in ("ClearDepthStencilView", "DiscardResource", "DiscardView", "DiscardView1", "CopyResource", "GenerateMips",
              "ResolveSubresource", "CopyStructureCount", "Begin", "End", "SetPredication", "Draw", "DrawInstanced",
              "DrawIndexed", "DrawIndexedInstanced", "DrawInstancedIndirect", "DrawIndexedInstancedIndirect", "DrawAuto",
              "Dispatch", "DispatchIndirect"):
    def _recorder(self, *args, _name=_name):
        if _name == "SetPredication":   # the predicate is state, and is used by the draws recorded after
            Context11.SetPredication(self, *args)
        self._record(_name, *args)
    setattr(DeferredContext11, _name, _recorder)

