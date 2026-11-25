# Fathom addition to dumpulator: Direct3D 12 for the emulated program. Boost Software License 1.0, like
# the rest of dumpulator (LICENSE.dumpulator).
#
# d3d12.dll needs a graphics driver the emulator does not have: D3D12CreateDevice gives COM objects
# implemented here. Command queues, allocators and lists (the commands are recorded with their arguments
# and run at ExecuteCommandLists), fences (Signal, SetEventOnCompletion setting the program's events),
# descriptor heaps and the descriptors (CBV / SRV / UAV / RTV / DSV / samplers), root signatures in their
# serialized form (D3D12SerializeRootSignature, versions 1.0 and 1.1, or embedded in a shader), graphics
# and compute pipeline states (shaders DXBC, run by dxbc.py through d3d_pipeline.py), committed and placed
# resources (buffers: their bytes in the emulator's memory, the GPU virtual address being that memory),
# GetCopyableFootprints, CopyTextureRegion / CopyBufferRegion / CopyResource, Map, render passes, bundles.
# Shaders compiled to DXIL (shader model 6, dxc) run in dxil.py; ray tracing (state objects, acceleration
# structures, DispatchRays) in dxr.py.

import ctypes
import struct
from typing import Dict, List, Optional

from . import dxbc, dxr
from .dxil import Shader as DxilShader
from .dxil_bitcode import BitcodeError
from .d3d11_emu import blends_of, input_layout
from .d3d_pipeline import DrawState, SOTarget, StreamOutput, dispatch, draw_dxbc
from .dxgi_emu import dxgi_of, missing, E_INVALIDARG, E_FAIL, DXGI_ERROR_NOT_FOUND, S_FALSE
from .gpu import DXGI, DXGI_SRGB, Image, Sampler, View, kind_bytes, pitch_of, rows_of, to_srgb
from .raster import numpy

E_NOTIMPL, E_NOINTERFACE, E_OUTOFMEMORY = 0x80004001, 0x80004002, 0x8007000E
INCREMENT = 32
HEAP_UPLOAD, HEAP_READBACK = 2, 3


# ------------------------------------------------------------------ structures (ctypes, per architecture)
def _layouts(x64: bool):
    ptr = ctypes.c_uint64 if x64 else ctypes.c_uint32
    u32, i32, f32, u64, u16, u8 = ctypes.c_uint32, ctypes.c_int32, ctypes.c_float, ctypes.c_uint64, ctypes.c_uint16, ctypes.c_uint8

    def make(name, fields):
        return type(name, (ctypes.Structure,), {"_fields_": fields})

    bytecode = make("Bytecode", [("p", ptr), ("size", ptr)])
    rt_blend = make("RTBlend", [("enable", u32), ("logic_enable", u32), ("src", u32), ("dst", u32), ("op", u32),
                                ("src_a", u32), ("dst_a", u32), ("op_a", u32), ("logic", u32), ("mask", u8)])
    layout = dict(
        bytecode=bytecode,
        graphics=make("GraphicsPSO", [
            ("root", ptr), ("vs", bytecode), ("ps", bytecode), ("ds", bytecode), ("hs", bytecode), ("gs", bytecode),
            ("so_decl", ptr), ("so_entries", u32), ("so_strides", ptr), ("so_num_strides", u32), ("so_stream", u32),
            ("alpha_to_coverage", u32), ("independent", u32), ("rt", rt_blend * 8), ("sample_mask", u32),
            ("fill", u32), ("cull", u32), ("front_ccw", u32), ("depth_bias", i32), ("bias_clamp", f32),
            ("slope_bias", f32), ("depth_clip", u32), ("multisample", u32), ("aa", u32), ("forced", u32),
            ("conservative", u32), ("depth_enable", u32), ("depth_write", u32), ("depth_func", u32),
            ("stencil", u32), ("stencil_read", u8), ("stencil_write", u8), ("front_face", u32 * 4),
            ("back_face", u32 * 4), ("layout", ptr), ("layout_count", u32), ("strip_cut", u32), ("topology_type", u32),
            ("rt_count", u32), ("rt_formats", u32 * 8), ("dsv_format", u32), ("sample_count", u32),
            ("sample_quality", u32), ("node_mask", u32), ("cached", bytecode), ("flags", u32)]),
        compute=make("ComputePSO", [("root", ptr), ("cs", bytecode), ("node_mask", u32), ("cached", bytecode),
                                    ("flags", u32)]),
        root_desc=make("RootDesc", [("count", u32), ("params", ptr), ("sampler_count", u32), ("samplers", ptr),
                                    ("flags", u32)]),
        root_param=make("RootParam", [("type", u32), ("a", u32), ("b", ptr), ("c", u32), ("visibility", u32)]),
        location=make("CopyLocation", [("resource", ptr), ("type", u32), ("offset", u64), ("format", u32),
                                       ("width", u32), ("height", u32), ("depth", u32), ("pitch", u32)]),
        resource_desc=make("ResourceDesc", [("dimension", u32), ("alignment", u64), ("width", u64), ("height", u32),
                                            ("depth", u16), ("mips", u16), ("format", u32), ("count", u32),
                                            ("quality", u32), ("layout", u32), ("flags", u32)]),
        rtv=make("RTVDesc", [("format", u32), ("dimension", u32), ("a", u64), ("b", u32), ("c", u32)]),
        dsv=make("DSVDesc", [("format", u32), ("dimension", u32), ("flags", u32), ("a", u32), ("b", u32), ("c", u32)]),
        srv=make("SRVDesc", [("format", u32), ("dimension", u32), ("mapping", u32), ("first", u64), ("count", u32),
                             ("stride", u32), ("flags", u32)]),
        uav=make("UAVDesc", [("format", u32), ("dimension", u32), ("first", u64), ("count", u32), ("stride", u32),
                             ("counter_offset", u64), ("flags", u32)]),
        cbv=make("CBVDesc", [("location", u64), ("size", u32)]),
        render_pass_rt=make("RenderPassRT", [("cpu", ptr), ("begin_type", u32), ("format", u32), ("color", f32 * 4),
                                             ("rest", u8 * 64)]),
    )
    # a copy location's union: placed footprint (u64 offset first) or a subresource index
    return layout


class Struct:
    def __init__(self, dp):
        self.dp = dp
        self.t = _layouts(dp._x64)

    def read(self, name, address):
        cls = self.t[name]
        return cls.from_buffer_copy(bytes(self.dp.read(address, ctypes.sizeof(cls))))

    def size(self, name):
        return ctypes.sizeof(self.t[name])


# ------------------------------------------------------------------ root signatures (RTS0)
class RootSignature:
    """parameters: dicts kind (table, constants, cbv, srv, uav), visibility, ranges / register / count;
    samplers: (register, Sampler, visibility)."""

    interface = "ID3D12RootSignature"

    def __init__(self, d3d, blob: bytes):
        self.d3d = d3d
        data = blob
        if blob[:4] == b"DXBC":
            parts = dxbc.chunks(blob)
            data = parts.get(b"RTS0", b"")
        self.data = bytes(data)
        self.parameters: List[dict] = []
        self.samplers: List[tuple] = []
        if len(data) >= 24:
            self._parse(data)

    def _parse(self, data):
        version, count, offset, sampler_count, sampler_offset, self.flags = struct.unpack_from("<6I", data, 0)
        for i in range(count):
            kind, visibility, payload = struct.unpack_from("<3I", data, offset + 12 * i)
            entry = dict(visibility=visibility)
            if kind == 0:
                ranges_count, ranges_offset = struct.unpack_from("<2I", data, payload)
                size = 24 if version >= 2 else 20
                ranges = []
                for k in range(ranges_count):
                    values = struct.unpack_from("<6I" if version >= 2 else "<5I", data, ranges_offset + size * k)
                    range_type, num, base, space = values[:4]
                    table_offset = values[-1]
                    ranges.append((range_type, num, base, space, table_offset))
                entry.update(kind="table", ranges=ranges)
            elif kind == 1:
                register, space, values = struct.unpack_from("<3I", data, payload)
                entry.update(kind="constants", register=register, space=space, count=values)
            else:
                register, space = struct.unpack_from("<2I", data, payload)
                entry.update(kind={2: "cbv", 3: "srv", 4: "uav"}.get(kind, "cbv"), register=register, space=space)
            self.parameters.append(entry)
        for i in range(sampler_count):
            values = struct.unpack_from("<4If2I4I", data, sampler_offset + 52 * i)
            filter_type, u, v, w, bias, anisotropy, compare, border = values[:8]
            min_lod, max_lod = struct.unpack_from("<2f", data, sampler_offset + 52 * i + 32)
            register, space, visibility = struct.unpack_from("<3I", data, sampler_offset + 52 * i + 40)
            sampler_space = space
            modes = {1: "wrap", 2: "mirror", 3: "clamp", 4: "border", 5: "mirror"}
            borders = {0: (0.0, 0.0, 0.0, 0.0), 1: (0.0, 0.0, 0.0, 1.0), 2: (1.0, 1.0, 1.0, 1.0)}
            sampler = Sampler(bool(filter_type & 0x4) or filter_type & 0x40 != 0, tuple(modes.get(m, "clamp") for m in (u, v, w)),
                              borders.get(border, (0.0, 0.0, 0.0, 0.0)), compare if filter_type & 0x80 else 0,
                              bool(filter_type & 1), min_lod, max_lod, bias)
            self.samplers.append((register, sampler, visibility, sampler_space))

    def GetDevice(self, riid, out):
        return self.d3d.device_query(riid, out)

    def SetName(self, name):
        return 0

    def SetPrivateData(self, guid, size, data):
        return 0

    def SetPrivateDataInterface(self, guid, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND


def shader_of(bytecode: bytes):
    """A DXBC shader, or a DXIL one (shader model 6: a DXIL part and no SHEX)."""
    parts = dxbc.chunks(bytecode)
    if b"DXIL" in parts and b"SHEX" not in parts and b"SHDR" not in parts:
        from .dxil_bitcode import BitcodeError
        try:
            return DxilShader(bytecode)
        except (BitcodeError, IndexError, KeyError, ValueError) as e:
            raise dxbc.Unsupported(f"DXIL: {e}")
    return dxbc.Shader(bytecode)


def serialize_root_signature(dp, address, version: int, layouts) -> bytes:
    """D3D12_ROOT_SIGNATURE_DESC (version 1) or D3D12_ROOT_SIGNATURE_DESC1 (2) -> RTS0 in a DXBC container."""
    x64 = dp._x64
    count, params = struct.unpack("<I", bytes(dp.read(address, 4)))[0], dp.read_ptr(address + (8 if x64 else 4))
    sampler_count = struct.unpack("<I", bytes(dp.read(address + (16 if x64 else 8), 4)))[0]
    samplers = dp.read_ptr(address + (24 if x64 else 12))
    flags = struct.unpack("<I", bytes(dp.read(address + (32 if x64 else 16), 4)))[0]
    param_size = 32 if x64 else 20
    header = 24
    payloads = bytearray()
    payload_base = header + 12 * count
    entries = []
    for i in range(count):
        base = params + param_size * i
        kind = struct.unpack("<I", bytes(dp.read(base, 4)))[0]
        union = base + (8 if x64 else 4)
        visibility = struct.unpack("<I", bytes(dp.read(base + (24 if x64 else 16), 4)))[0]
        offset = payload_base + len(payloads)
        if kind == 0:
            ranges_count = struct.unpack("<I", bytes(dp.read(union, 4)))[0]
            ranges = dp.read_ptr(union + (8 if x64 else 4))
            size = 24 if version >= 2 else 20
            payloads += struct.pack("<2I", ranges_count, offset + 8)
            payloads += bytes(dp.read(ranges, size * ranges_count))
        elif kind == 1:
            payloads += bytes(dp.read(union, 12))
        else:
            payloads += bytes(dp.read(union, 12 if version >= 2 else 8))
        entries.append(struct.pack("<3I", kind, visibility, offset))
    sampler_offset = payload_base + len(payloads)
    sampler_data = bytes(dp.read(samplers, 52 * sampler_count)) if sampler_count else b""
    body = struct.pack("<6I", version, count, header, sampler_count, sampler_offset, flags) + b"".join(entries) + \
        bytes(payloads) + sampler_data
    chunk = b"RTS0" + struct.pack("<I", len(body)) + body
    total = 32 + 4 + len(chunk)
    return b"DXBC" + b"\0" * 16 + struct.pack("<3I", 1, total, 1) + struct.pack("<I", 36) + chunk


class Blob:
    interface = "ID3D10Blob"
    interfaces = ["ID3DBlob"]

    def __init__(self, d3d, data: bytes):
        self.d3d = d3d
        self.size = len(data)
        self.address = d3d.dp.allocate(max(16, len(data)))
        d3d.dp.write(self.address, data)

    def GetBufferPointer(self):
        return self.address

    def GetBufferSize(self):
        return self.size


# ------------------------------------------------------------------ resources and descriptors
class Resource12:
    interface = "ID3D12Resource"
    interfaces = ["ID3D12Resource1"]
    WIDE = {"GetGPUVirtualAddress"}

    def __init__(self, d3d, desc, heap_type=1, state=0):
        self.d3d, self.heap_type, self.state = d3d, heap_type, state
        self.desc = bytes(desc)
        s = d3d.s.t["resource_desc"].from_buffer_copy(self.desc.ljust(ctypes.sizeof(d3d.s.t["resource_desc"]), b"\0"))
        self.dimension = {1: "buffer", 2: "1d", 3: "2d", 4: "3d"}.get(s.dimension, "buffer")
        self.width, self.height, self.depth, self.format, self.flags = int(s.width), s.height or 1, s.depth or 1, s.format, s.flags
        self.mips = s.mips or 1
        self.images: List[List[Image]] = []
        self.address = 0
        self.size = 0
        if self.dimension == "buffer":
            self.size = self.width
            self.address = d3d.dp.allocate(max(256, self.size), page_align=True)
            d3d.buffers.append(self)
        else:
            if not s.mips:
                self.mips = 1 + max(self.width, self.height).bit_length() - 1
            kind = DXGI.get(self.format, "rgba8")
            slices = self.depth if self.dimension != "3d" else self.depth
            for _ in range(max(1, slices)):
                levels = [Image(max(1, self.width >> m), max(1, self.height >> m), kind)
                          for m in range(self.mips if self.dimension != "3d" else 1)]
                self.images.append(levels)

    def subresource(self, index: int) -> Optional[Image]:
        if not self.images:
            return None
        mips = len(self.images[0])
        s, m = index // mips, index % mips
        return self.images[s][m] if s < len(self.images) else None

    def read(self) -> numpy.ndarray:
        return numpy.frombuffer(bytearray(self.d3d.dp.read(self.address, self.size)), dtype=numpy.uint8)

    def write(self, data, offset=0):
        self.d3d.dp.write(self.address + offset, bytes(data))

    def Map(self, sub, read_range, out):
        if self.dimension != "buffer":
            image = self.subresource(sub)
            if image is None:
                return E_INVALIDARG
            if out:
                self.d3d.dp.write_ptr(out, image.map(self.d3d.dp))
            return 0
        if out:
            self.d3d.dp.write_ptr(out, self.address)
        return 0

    def Unmap(self, sub, written):
        if self.dimension != "buffer":
            image = self.subresource(sub)
            if image is not None:
                image.unmap(self.d3d.dp)

    def GetDesc(self, out):
        self.d3d.dp.write(out, self.desc)
        return out

    def GetGPUVirtualAddress(self):
        return self.address

    def WriteToSubresource(self, sub, box, data, row_pitch, depth_pitch):
        image = self.subresource(sub)
        if image is None:
            return E_INVALIDARG
        dp = self.d3d.dp
        area = struct.unpack("<6I", bytes(dp.read(box, 24))) if box else None
        rect = (area[0], area[1], area[3], area[4]) if area else None
        rows = rows_of(image.kind, (area[4] - area[1]) if area else image.height)
        image.from_bytes(bytes(dp.read(data, row_pitch * rows)), row_pitch, rect)

    def ReadFromSubresource(self, data, row_pitch, depth_pitch, sub, box):
        image = self.subresource(sub)
        if image is None:
            return E_INVALIDARG
        raw = image.to_bytes()
        pitch = image.pitch
        rows = rows_of(image.kind, image.height)
        dp = self.d3d.dp
        for r in range(rows):
            dp.write(data + r * row_pitch, raw[r * pitch:(r + 1) * pitch])

    def GetHeapProperties(self, props, flags):
        dp = self.d3d.dp
        if props:
            dp.write(props, struct.pack("<5I", self.heap_type, 0, 0, 1, 1))
        if flags:
            dp.write(flags, struct.pack("<I", 0))

    def GetProtectedResourceSession(self, riid, out):
        self.d3d.dp.write_ptr(out, 0)
        return E_FAIL

    def GetDevice(self, riid, out):
        return self.d3d.device_query(riid, out)

    def SetName(self, name):
        return 0

    def SetPrivateData(self, guid, size, data):
        return 0

    def SetPrivateDataInterface(self, guid, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND


class Descriptor:
    """What a descriptor holds: kind (cbv, srv, uav, rtv, dsv, sampler) and its values."""

    def __init__(self, kind, resource=None, **values):
        self.kind, self.resource, self.values = kind, resource, values


class DescriptorHeap:
    interface = "ID3D12DescriptorHeap"

    def __init__(self, d3d, kind, count, flags):
        self.d3d, self.kind, self.count, self.flags = d3d, kind, max(1, count), flags
        index = len(d3d.heaps)
        self.cpu = 0x10000000 + index * 0x400000
        self.gpu = 0x100000000 + index * 0x400000
        self.descriptors: List[Optional[Descriptor]] = [None] * self.count
        d3d.heaps.append(self)

    def GetDesc(self, out):
        self.d3d.dp.write(out, struct.pack("<4I", self.kind, self.count, self.flags, 0))
        return out

    def GetCPUDescriptorHandleForHeapStart(self, out):
        self.d3d.dp.write_ptr(out, self.cpu)
        return out

    def GetGPUDescriptorHandleForHeapStart(self, out):
        self.d3d.dp.write(out, struct.pack("<Q", self.gpu))
        return out

    def GetDevice(self, riid, out):
        return self.d3d.device_query(riid, out)

    def SetName(self, name):
        return 0

    def SetPrivateData(self, guid, size, data):
        return 0

    def SetPrivateDataInterface(self, guid, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND


class PipelineState:
    interface = "ID3D12PipelineState"

    def __init__(self, d3d):
        self.d3d = d3d
        self.shaders: Dict[str, object] = {}
        self.layout: List[dict] = []
        self.blends = []
        self.rasterizer = (3, 3, False, True, True)
        self.depth_state = (False, False, 2)
        self.root: Optional[RootSignature] = None
        self.compute = False
        self.error = None
        self.so = None              # stream output: (entries, strides, rasterized stream)

    def GetCachedBlob(self, out):
        blob = Blob(self.d3d, b"")
        self.d3d.dp.write_ptr(out, self.d3d.com.create(blob, "ID3D10Blob"))

    def GetDevice(self, riid, out):
        return self.d3d.device_query(riid, out)

    def SetName(self, name):
        return 0

    def SetPrivateData(self, guid, size, data):
        return 0

    def SetPrivateDataInterface(self, guid, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND


class Fence:
    interface = "ID3D12Fence"
    interfaces = ["ID3D12Fence1"]
    WIDE = {"GetCompletedValue"}

    def __init__(self, d3d, value):
        self.d3d, self.value = d3d, value
        self.waiting: List[tuple] = []

    def GetCompletedValue(self):
        return self.value

    def set(self, value):
        self.value = value
        still = []
        for target, event in self.waiting:
            if self.value >= target:
                self.d3d.set_event(event)
            else:
                still.append((target, event))
        self.waiting = still

    def SetEventOnCompletion(self, value, event):
        if self.value >= value:
            self.d3d.set_event(event)
        else:
            self.waiting.append((value, event))

    def Signal(self, value):
        self.set(value)

    def GetCreationFlags(self):
        return 0

    def GetDevice(self, riid, out):
        return self.d3d.device_query(riid, out)

    def SetName(self, name):
        return 0

    def SetPrivateData(self, guid, size, data):
        return 0

    def SetPrivateDataInterface(self, guid, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND


class Simple:
    """Command allocators, heaps, query heaps, command signatures."""

    def __init__(self, d3d, interface, data=None):
        self.d3d, self.interface, self.data = d3d, interface, data

    def Reset(self):
        return 0

    def GetDesc(self, out):
        if self.data:
            self.d3d.dp.write(out, self.data)
        return out

    def GetDevice(self, riid, out):
        return self.d3d.device_query(riid, out)

    def SetName(self, name):
        return 0

    def SetPrivateData(self, guid, size, data):
        return 0

    def SetPrivateDataInterface(self, guid, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND

    def GetProtectedResourceSession(self, riid, out):
        self.d3d.dp.write_ptr(out, 0)
        return E_FAIL


# ------------------------------------------------------------------ command lists
class CommandList:
    interface = "ID3D12GraphicsCommandList"
    interfaces = [f"ID3D12GraphicsCommandList{k}" for k in range(1, 11)]

    def __init__(self, d3d, kind, pso, closed=False):
        self.d3d, self.kind = d3d, kind
        self.commands: List[tuple] = []
        self.closed = closed
        self.initial = pso
        if pso is not None:
            self.commands.append(("pso", pso))

    def _record(self, *command):
        self.commands.append(command)

    def Close(self):
        self.closed = True
        return 0

    def Reset(self, allocator, pso):
        self.commands = []
        self.closed = False
        impl = self.d3d.com.impl(pso) if pso else None
        if impl is not None:
            self.commands.append(("pso", impl))
        return 0

    def ClearState(self, pso):
        self._record("clear_state", self.d3d.com.impl(pso) if pso else None)

    def GetType(self):
        return self.kind

    def GetDevice(self, riid, out):
        return self.d3d.device_query(riid, out)

    def SetName(self, name):
        return 0

    def SetPrivateData(self, guid, size, data):
        return 0

    def SetPrivateDataInterface(self, guid, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND

    # recorded with their values
    def DrawInstanced(self, vertices, instances, start, start_instance):
        self._record("draw", vertices, instances, start, start_instance)

    def DrawIndexedInstanced(self, indices, instances, start, base, start_instance):
        self._record("draw_indexed", indices, instances, start, base, start_instance)

    def Dispatch(self, x, y, z):
        self._record("dispatch", x, y, z)

    def DispatchMesh(self, x, y, z):
        self._record("dispatch_mesh", x, y, z)

    def SetProgram(self, desc):
        dp = self.d3d.dp
        kind = struct.unpack("<I", bytes(dp.read(desc, 4)))[0]
        if kind == 5:       # WORK_GRAPH: its program identifier
            ident = bytes(dp.read(desc + 8, 32))
            self._record("program", self.d3d.programs.get(ident))
        else:
            missing(dp, f"Direct3D 12 SetProgram of type {kind}")

    def DispatchGraph(self, desc):
        """The records of the entry points: copied now from the CPU, read at execution from the GPU."""
        dp = self.d3d.dp
        p = dp.ptr_size()
        mode = struct.unpack("<I", bytes(dp.read(desc, 4)))[0]

        def cpu_input(at):
            entry, count = struct.unpack("<2I", bytes(dp.read(at, 8)))
            records = dp.read_ptr(at + 8)
            stride = struct.unpack("<Q", bytes(dp.read(at + 16, 8)))[0]
            return entry, [bytes(dp.read(records + k * stride, stride)) if stride else b"" for k in range(count)]
        if mode == 0:
            self._record("dispatch_graph", [cpu_input(desc + 8)], None)
        elif mode == 2:
            count = struct.unpack("<I", bytes(dp.read(desc + 8, 4)))[0]
            inputs = dp.read_ptr(desc + (16 if p == 8 else 12))
            stride = struct.unpack("<Q", bytes(dp.read(desc + (24 if p == 8 else 16), 8)))[0] or (32 if p == 8 else 24)
            self._record("dispatch_graph", [cpu_input(inputs + k * stride) for k in range(count)], None)
        else:
            self._record("dispatch_graph", [], (mode, struct.unpack("<Q", bytes(dp.read(desc + 8, 8)))[0]))

    def CopyBufferRegion(self, dst, dst_offset, src, src_offset, size):
        self._record("copy_buffer", self.d3d.com.impl(dst), dst_offset, self.d3d.com.impl(src), src_offset, size)

    def CopyTextureRegion(self, dst, x, y, z, src, box):
        dp = self.d3d.dp
        area = struct.unpack("<6I", bytes(dp.read(box, 24))) if box else None
        self._record("copy_texture", self.d3d.location(dst), x, y, z, self.d3d.location(src), area)

    def CopyResource(self, dst, src):
        self._record("copy_resource", self.d3d.com.impl(dst), self.d3d.com.impl(src))

    def ResolveSubresource(self, dst, dst_sub, src, src_sub, fmt):
        self._record("resolve", self.d3d.com.impl(dst), dst_sub, self.d3d.com.impl(src), src_sub)

    def IASetPrimitiveTopology(self, topology):
        self._record("topology", topology)

    def RSSetViewports(self, count, viewports):
        dp = self.d3d.dp
        self._record("viewports", [struct.unpack("<6f", bytes(dp.read(viewports + 24 * i, 24))) for i in range(count)])

    def RSSetScissorRects(self, count, rects):
        dp = self.d3d.dp
        self._record("scissors", [struct.unpack("<4i", bytes(dp.read(rects + 16 * i, 16))) for i in range(count)])

    def OMSetBlendFactor(self, factor):
        value = struct.unpack("<4f", bytes(self.d3d.dp.read(factor, 16))) if factor else (1.0, 1.0, 1.0, 1.0)
        self._record("blend_factor", value)

    def OMSetStencilRef(self, value):
        return None

    def OMSetDepthBounds(self, low, high):
        return None

    def SetPipelineState(self, pso):
        self._record("pso", self.d3d.com.impl(pso))

    def ResourceBarrier(self, count, barriers):
        return None

    def ExecuteBundle(self, bundle):
        impl = self.d3d.com.impl(bundle)
        if impl is not None:
            self._record("bundle", impl)

    def SetDescriptorHeaps(self, count, heaps):
        dp = self.d3d.dp
        self._record("heaps", [self.d3d.com.impl(dp.read_ptr(heaps + dp.ptr_size() * i)) for i in range(count)])

    def SetComputeRootSignature(self, root):
        self._record("root", "compute", self.d3d.com.impl(root))

    def SetGraphicsRootSignature(self, root):
        self._record("root", "graphics", self.d3d.com.impl(root))

    def _root_arg(self, kind, index, value):
        self._record("root_arg", kind, index, value)

    def SetComputeRootDescriptorTable(self, index, handle):
        self._root_arg("compute", index, ("table", handle))

    def SetGraphicsRootDescriptorTable(self, index, handle):
        self._root_arg("graphics", index, ("table", handle))

    def SetComputeRoot32BitConstant(self, index, value, offset):
        self._record("root_constants", "compute", index, offset, [value & 0xFFFFFFFF])

    def SetGraphicsRoot32BitConstant(self, index, value, offset):
        self._record("root_constants", "graphics", index, offset, [value & 0xFFFFFFFF])

    def SetComputeRoot32BitConstants(self, index, count, data, offset):
        values = list(struct.unpack(f"<{count}I", bytes(self.d3d.dp.read(data, 4 * count)))) if count else []
        self._record("root_constants", "compute", index, offset, values)

    def SetGraphicsRoot32BitConstants(self, index, count, data, offset):
        values = list(struct.unpack(f"<{count}I", bytes(self.d3d.dp.read(data, 4 * count)))) if count else []
        self._record("root_constants", "graphics", index, offset, values)

    def SetComputeRootConstantBufferView(self, index, address):
        self._root_arg("compute", index, ("address", address))

    def SetGraphicsRootConstantBufferView(self, index, address):
        self._root_arg("graphics", index, ("address", address))

    SetComputeRootShaderResourceView = SetComputeRootConstantBufferView
    SetGraphicsRootShaderResourceView = SetGraphicsRootConstantBufferView
    SetComputeRootUnorderedAccessView = SetComputeRootConstantBufferView
    SetGraphicsRootUnorderedAccessView = SetGraphicsRootConstantBufferView

    def IASetIndexBuffer(self, view):
        if not view:
            self._record("index_buffer", None)
            return
        address, size, fmt = struct.unpack("<QII", bytes(self.d3d.dp.read(view, 16)))
        self._record("index_buffer", (address, size, fmt))

    def IASetVertexBuffers(self, start, count, views):
        dp = self.d3d.dp
        entries = [struct.unpack("<QII", bytes(dp.read(views + 16 * i, 16))) if views else None for i in range(count)]
        self._record("vertex_buffers", start, entries)

    def SOSetTargets(self, start, count, views):
        dp = self.d3d.dp
        entries = [struct.unpack("<3Q", bytes(dp.read(views + 24 * i, 24))) if views else None for i in range(count)]
        self._record("so_targets", start, entries)

    def OMSetRenderTargets(self, count, handles, single, dsv):
        dp = self.d3d.dp
        ptr = dp.ptr_size()
        if single:
            first = dp.read_ptr(handles) if handles else 0
            rtvs = [first + INCREMENT * i for i in range(count)]
        else:
            rtvs = [dp.read_ptr(handles + ptr * i) for i in range(count)] if handles else []
        depth = dp.read_ptr(dsv) if dsv else 0
        self._record("targets", rtvs, depth)

    def ClearDepthStencilView(self, handle, flags, depth, stencil, count, rects):
        self._record("clear_depth", handle, flags, depth)

    def ClearRenderTargetView(self, handle, color, count, rects):
        rgba = struct.unpack("<4f", bytes(self.d3d.dp.read(color, 16)))
        self._record("clear_rtv", handle, rgba)

    def ClearUnorderedAccessViewUint(self, gpu, cpu, resource, values, count, rects):
        self._record("clear_uav", cpu, struct.unpack("<4I", bytes(self.d3d.dp.read(values, 16))))

    def ClearUnorderedAccessViewFloat(self, gpu, cpu, resource, values, count, rects):
        self._record("clear_uav", cpu, struct.unpack("<4f", bytes(self.d3d.dp.read(values, 16))))

    def DiscardResource(self, resource, region):
        return None

    def BeginQuery(self, heap, kind, index):
        return None

    def EndQuery(self, heap, kind, index):
        return None

    def ResolveQueryData(self, heap, kind, start, count, dst, offset):
        self._record("resolve_query", self.d3d.com.impl(dst), offset, count)

    def SetPredication(self, buffer, offset, op):
        return None

    def SetMarker(self, metadata, data, size):
        return None

    def BeginEvent(self, metadata, data, size):
        return None

    def EndEvent(self):
        return None

    def ExecuteIndirect(self, signature, count, arguments, offset, counter, counter_offset):
        self._record("indirect", self.d3d.com.impl(signature), count, self.d3d.com.impl(arguments), offset)

    def AtomicCopyBufferUINT(self, *args):
        missing(self.d3d.dp, "Direct3D 12 AtomicCopyBuffer")

    AtomicCopyBufferUINT64 = AtomicCopyBufferUINT

    def SetSamplePositions(self, *args):
        return None

    def SetViewInstanceMask(self, mask):
        return None

    def WriteBufferImmediate(self, count, parameters, modes):
        dp = self.d3d.dp
        values = [struct.unpack("<QI", bytes(dp.read(parameters + 16 * i, 12))) for i in range(count)]
        self._record("write_immediate", values)

    def SetProtectedResourceSession(self, session):
        return None

    def BeginRenderPass(self, count, rts, depth, flags):
        dp = self.d3d.dp
        size = ctypes.sizeof(self.d3d.s.t["render_pass_rt"])
        rtvs, clears = [], []
        for i in range(count):
            desc = self.d3d.s.read("render_pass_rt", rts + size * i)
            rtvs.append(desc.cpu)
            clears.append((desc.cpu, tuple(desc.color)) if desc.begin_type == 2 else None)
        dsv, depth_clear = 0, None
        if depth:
            dsv = dp.read_ptr(depth)
            begin = struct.unpack("<I", bytes(dp.read(depth + dp.ptr_size(), 4)))[0]
            if begin == 2:
                value = struct.unpack("<f", bytes(dp.read(depth + dp.ptr_size() + 8, 4)))[0]
                depth_clear = (dsv, value)
        self._record("targets", rtvs, dsv)
        for clear in clears:
            if clear is not None:
                self._record("clear_rtv", clear[0], clear[1])
        if depth_clear is not None:
            self._record("clear_depth", depth_clear[0], 1, depth_clear[1])

    def EndRenderPass(self):
        return None

    def InitializeMetaCommand(self, *args):
        return None

    def ExecuteMetaCommand(self, *args):
        return None

    def BuildRaytracingAccelerationStructure(self, desc, count, postbuild):
        self._record("build_structure", dxr.read_build(self.d3d.dp, desc))

    def EmitRaytracingAccelerationStructurePostbuildInfo(self, *args):
        return None

    def CopyRaytracingAccelerationStructure(self, dst, src, mode):
        self._record("copy_structure", dst, src)

    def SetPipelineState1(self, state):
        self._record("state_object", self.d3d.com.impl(state))

    def DispatchRays(self, desc):
        self._record("dispatch_rays", dxr.read_dispatch(self.d3d.dp, desc))


class Executor:
    """The state while the commands of lists run on a queue."""

    def __init__(self, d3d):
        self.d3d = d3d
        self.pso: Optional[PipelineState] = None
        self.roots = {"graphics": None, "compute": None}
        self.args = {"graphics": {}, "compute": {}}
        self.topology = 4
        self.viewports, self.scissors = [], []
        self.blend_factor = (1.0, 1.0, 1.0, 1.0)
        self.index_buffer = None
        self.vertex_buffers: Dict[int, tuple] = {}
        self.rtvs: List[int] = []
        self.dsv = 0
        self.so_targets: Dict[int, tuple] = {}     # slot -> (location, size, filled size location)
        self.state_object = None
        self.program = None                         # the work graph SetProgram chose: (state object, graph)
        self.heaps = {}                             # heap type (0 CBV/SRV/UAV, 1 sampler) -> DescriptorHeap

    def run(self, commands):
        for command in commands:
            name = command[0]
            handler = getattr(self, "_" + name, None)
            if handler is not None:
                try:
                    handler(*command[1:])
                except dxbc.Unsupported as e:
                    missing(self.d3d.dp, f"Direct3D 12 shader: {e}")

    def _pso(self, pso):
        self.pso = pso

    def _so_targets(self, start, entries):
        for i, entry in enumerate(entries):
            self.so_targets[start + i] = entry if entry is not None and entry[0] else None

    def _clear_state(self, pso):
        self.__init__(self.d3d)
        self.pso = pso

    def _bundle(self, bundle):
        self.run(bundle.commands)

    def _root(self, kind, root):
        if root is not self.roots[kind]:
            self.args[kind] = {}
        self.roots[kind] = root

    def _root_arg(self, kind, index, value):
        self.args[kind][index] = value

    def _root_constants(self, kind, index, offset, values):
        current = self.args[kind].get(index)
        array = list(current[1]) if current and current[0] == "constants" else []
        if len(array) < offset + len(values):
            array += [0] * (offset + len(values) - len(array))
        array[offset:offset + len(values)] = values
        self.args[kind][index] = ("constants", array)

    def _topology(self, topology):
        self.topology = topology

    def _viewports(self, viewports):
        self.viewports = viewports

    def _scissors(self, scissors):
        self.scissors = scissors

    def _blend_factor(self, value):
        self.blend_factor = value

    def _index_buffer(self, entry):
        self.index_buffer = entry

    def _vertex_buffers(self, start, entries):
        for i, entry in enumerate(entries):
            self.vertex_buffers[start + i] = entry

    def _targets(self, rtvs, dsv):
        self.rtvs, self.dsv = rtvs, dsv

    def _clear_rtv(self, handle, rgba):
        descriptor = self.d3d.descriptor(handle)
        if descriptor is None or descriptor.resource is None:
            return
        image = self.d3d.view_image(descriptor)
        if image is not None:
            if descriptor.values.get("format", 0) in DXGI_SRGB:
                rgba = tuple(to_srgb(numpy.array([rgba], dtype=numpy.float32))[0])
            image.clear(rgba)

    def _clear_depth(self, handle, flags, depth):
        descriptor = self.d3d.descriptor(handle)
        if descriptor is not None and flags & 1:
            image = self.d3d.view_image(descriptor)
            if image is not None:
                image.fb.depth[:] = depth
                image.changed()

    def _clear_uav(self, handle, values):
        descriptor = self.d3d.descriptor(handle)
        if descriptor is None or descriptor.resource is None:
            return
        resource = descriptor.resource
        if resource.dimension == "buffer":
            first = descriptor.values.get("first", 0) * max(4, descriptor.values.get("stride", 0) or 4)
            count = descriptor.values.get("count", 0) * max(4, descriptor.values.get("stride", 0) or 4)
            resource.write(numpy.full(count // 4, int(values[0]) & 0xFFFFFFFF, dtype="<u4").tobytes(), first)
        else:
            image = self.d3d.view_image(descriptor)
            if image is not None:
                image.set_texels(numpy.broadcast_to(numpy.array(values, dtype=numpy.float32), image.texels().shape).copy())

    def _write_immediate(self, values):
        for address, value in values:
            found = self.d3d.at_address(address)
            if found is not None:
                found[0].write(struct.pack("<I", value), found[1])

    def _resolve_query(self, buffer, offset, count):
        if buffer is not None:
            buffer.write(b"\0" * 8 * count, offset)

    def _copy_buffer(self, dst, dst_offset, src, src_offset, size):
        if dst is not None and src is not None:
            dst.write(src.read()[src_offset:src_offset + size], dst_offset)

    def _copy_resource(self, dst, src):
        if dst is None or src is None:
            return
        if dst.dimension == "buffer" and src.dimension == "buffer":
            dst.write(src.read()[:dst.size])
            return
        for a_levels, b_levels in zip(dst.images, src.images):
            for a, b in zip(a_levels, b_levels):
                a.copy_from(b)

    def _resolve(self, dst, dst_sub, src, src_sub):
        if dst is not None and src is not None:
            a, b = dst.subresource(dst_sub), src.subresource(src_sub)
            if a is not None and b is not None:
                a.copy_from(b)

    def _copy_texture(self, dst, x, y, z, src, area):
        """CopyTextureRegion: buffer (placed footprint) <-> texture (subresource), texture <-> texture."""
        dst_resource, dst_kind, dst_value = dst
        src_resource, src_kind, src_value = src
        if dst_resource is None or src_resource is None:
            return
        if src_kind == 1 and dst_kind == 0:     # upload: buffer -> texture
            image = dst_resource.subresource(dst_value)
            offset, fmt, width, height, depth, pitch = src_value
            if image is None:
                return
            x0, y0 = (area[0], area[1]) if area else (0, 0)
            x1, y1 = (area[3], area[4]) if area else (width, height)
            rows = rows_of(image.kind, y1 - y0)
            data = src_resource.read()[offset + y0 * pitch:offset + (y0 + rows) * pitch].tobytes()
            bpp = kind_bytes(image.kind)
            if x0:
                data = b"".join(data[r * pitch + x0 * bpp:(r + 1) * pitch] for r in range(rows))
                pitch = pitch - x0 * bpp
            image.from_bytes(data, pitch, (x, y, x + x1 - x0, y + y1 - y0) if (x, y, x1 - x0, y1 - y0) != (0, 0, image.width, image.height) else None)
        elif src_kind == 0 and dst_kind == 1:   # readback: texture -> buffer
            image = src_resource.subresource(src_value)
            offset, fmt, width, height, depth, pitch = dst_value
            if image is None:
                return
            raw = image.to_bytes()
            row = image.pitch
            rows = rows_of(image.kind, image.height)
            out = bytearray()
            for r in range(rows):
                out += raw[r * row:(r + 1) * row].ljust(pitch, b"\0")
            dst_resource.write(bytes(out[:len(out)]), offset)
        elif src_kind == 0 and dst_kind == 0:
            a, b = dst_resource.subresource(dst_value), src_resource.subresource(src_value)
            if a is not None and b is not None:
                a.copy_from(b, (area[0], area[1], area[3], area[4]) if area else None, (x, y))
        else:                                   # buffer footprint -> buffer footprint
            dst_resource.write(src_resource.read()[src_value[0]:], dst_value[0])

    # drawing ----------------------------------------------------------------
    def _bindings(self, kind, shader, stage_visibility, views=None):
        """The bindings of a shader from the root signature and its arguments (the views it may write added to
        views, also those of the heaps resolved later, while the shader runs)."""
        root = self.roots[kind]
        bindings = dxbc.Bindings()
        views: List[View] = [] if views is None else views
        resolved = {}

        def heap_resource(index, sampler):
            """A slot of the bound heaps (ResourceDescriptorHeap[index] / SamplerDescriptorHeap[index])."""
            key = (index, sampler)
            if key not in resolved:
                heap = self.heaps.get(1 if sampler else 0)
                descriptor = heap.descriptors[index] if heap is not None and 0 <= index < heap.count else None
                found = None
                if descriptor is not None and descriptor.kind == "sampler":
                    found = descriptor.values["sampler"]
                elif descriptor is not None and descriptor.kind == "cbv":
                    place = self.d3d.at_address(descriptor.values["location"])
                    if place is not None:
                        data = place[0].read()[place[1]:place[1] + descriptor.values["size"]]
                        data = numpy.concatenate([data, numpy.zeros(-len(data) % 16, dtype=numpy.uint8)])
                        found = data.view("<u4").reshape(-1, 4)
                elif descriptor is not None:
                    found = self.d3d.gpu_view(descriptor)
                    if found is not None:
                        views.append(found)
                resolved[key] = found
            return resolved[key]
        bindings.heap_resource = heap_resource
        if root is None:
            return bindings, views
        self._bind_root(root, self.args[kind], shader, stage_visibility, views, bindings)
        return bindings, views

    def local_bindings(self, root, record: bytes, shader, base, views):
        """The bindings of a ray tracing shader: those of the global root signature (base) and the local root
        arguments of its shader record (after the 32-byte identifier), as the local root signature lays them out."""
        bindings = dxbc.Bindings(dict(base.cbs), dict(base.srvs), dict(base.samplers), dict(base.uavs))
        bindings.heap_resource = getattr(base, "heap_resource", None)
        if root is None:
            return bindings
        args = {}
        offset = 32
        for index, parameter in enumerate(root.parameters):
            if parameter["kind"] == "constants":
                offset += -offset % 4
                count = parameter["count"]
                args[index] = ("constants", list(struct.unpack_from(f"<{count}I", record, offset)) if len(record) >= offset + 4 * count else [])
                offset += 4 * count
            else:
                offset += -offset % 8
                value = struct.unpack_from("<Q", record, offset)[0] if len(record) >= offset + 8 else 0
                args[index] = ("table" if parameter["kind"] == "table" else "address", value)
                offset += 8
        self._bind_root(root, args, shader, 0, views, bindings)
        return bindings

    def _bind_root(self, root, arguments, shader, stage_visibility, views, bindings):
        """The resources a root signature binds with its arguments, put in bindings by register (and by (space,
        register) for a space other than 0: DXIL looks them up with their space)."""
        def put(table, space, register, value):
            if space:
                table[(space, register)] = value
                table.setdefault(register, value)
            else:
                table[register] = value
        for index, parameter in enumerate(root.parameters):
            visibility = parameter["visibility"]
            if visibility not in (0, stage_visibility):
                continue
            arg = arguments.get(index)
            space = parameter.get("space", 0)
            if parameter["kind"] == "constants":
                values = list(arg[1]) if arg and arg[0] == "constants" else []
                values += [0] * (-len(values) % 4)
                if values:
                    put(bindings.cbs, space, parameter["register"], numpy.array(values, dtype=numpy.uint32).reshape(-1, 4))
                continue
            if arg is None:
                continue
            if parameter["kind"] in ("cbv", "srv", "uav"):
                found = self.d3d.at_address(arg[1])
                if found is None:
                    continue
                resource, offset = found
                register = parameter["register"]
                if parameter["kind"] == "cbv":
                    data = resource.read()[offset:offset + 65536]
                    data = numpy.concatenate([data, numpy.zeros(-len(data) % 16, dtype=numpy.uint8)])
                    put(bindings.cbs, space, register, data.view("<u4").reshape(-1, 4))
                else:
                    declared = (shader.resources if parameter["kind"] == "srv" else shader.uavs).get(register, {})
                    stride = declared.get("stride", 0)
                    view = View("structured" if stride else "raw", stride=stride,
                                count=(resource.size - offset) // max(1, stride or 4))
                    view.bytes = resource.read()[offset:].copy()
                    view.source = (resource, offset)
                    view.address = arg[1]       # an acceleration structure (root SRV)
                    view.structures = self.d3d.structures
                    put(bindings.srvs if parameter["kind"] == "srv" else bindings.uavs, space, register, view)
                    views.append(view)
                continue
            # a descriptor table
            heap, start = self.d3d.gpu_heap(arg[1])
            if heap is None:
                continue
            running = 0
            for range_type, num, base, space, table_offset in parameter["ranges"]:
                if table_offset != 0xFFFFFFFF:
                    running = table_offset
                count = num if num != 0xFFFFFFFF else max(0, heap.count - start - running)
                for k in range(min(count, 128)):
                    slot = start + running + k
                    descriptor = heap.descriptors[slot] if 0 <= slot < heap.count else None
                    register = base + k
                    if descriptor is None:
                        continue
                    if range_type == 3:
                        put(bindings.samplers, space, register, descriptor.values["sampler"])
                    elif range_type == 2:
                        found = self.d3d.at_address(descriptor.values["location"])
                        if found is not None:
                            resource, offset = found
                            data = resource.read()[offset:offset + descriptor.values["size"]]
                            data = numpy.concatenate([data, numpy.zeros(-len(data) % 16, dtype=numpy.uint8)])
                            put(bindings.cbs, space, register, data.view("<u4").reshape(-1, 4))
                    else:
                        view = self.d3d.gpu_view(descriptor)
                        if view is not None:
                            put(bindings.srvs if range_type == 0 else bindings.uavs, space, register, view)
                            views.append(view)
                running += count
        for register, sampler, visibility, *rest in root.samplers:
            if visibility in (0, stage_visibility):
                put(bindings.samplers, rest[0] if rest else 0, register, sampler)
        return bindings

    def _write_back(self, views):
        for view in views:
            counter = getattr(view, "counter_source", None)
            if counter is not None and view.counter is not None:
                counter[0].write(struct.pack("<I", view.counter[0] & 0xFFFFFFFF), counter[1])
            source = getattr(view, "source", None)
            if view.bytes is None or not view.dirty or source is None:
                continue
            if isinstance(source, tuple):
                resource, offset = source
                resource.write(view.bytes, offset)
            elif isinstance(source, Descriptor) and source.resource is not None:
                source.resource.write(view.bytes)

    def _state(self) -> Optional[DrawState]:
        pso = self.pso
        if pso is None or pso.compute:
            return None
        if pso.error:
            missing(self.d3d.dp, f"Direct3D 12 pipeline: {pso.error}")
            return None
        state = DrawState()
        state.shaders = dict(pso.shaders)
        state.layout = pso.layout
        if pso.so is not None:
            entries, strides, rasterized = pso.so
            state.stream_out = StreamOutput(entries, strides, rasterized)
            for slot, target in self.so_targets.items():
                if target is None or slot >= 4:
                    continue
                location, size, filled_location = target
                found, filled = self.d3d.at_address(location), self.d3d.at_address(filled_location)
                if found is None:
                    continue
                resource, offset = found
                used = 0
                if filled is not None:
                    used = struct.unpack_from("<Q", resource.read().tobytes() if filled[0] is resource else
                                              filled[0].read().tobytes(), filled[1])[0]
                so_target = SOTarget(resource.read(), offset + used, offset + size, resource)
                so_target.base, so_target.filled = offset, filled
                state.stream_out.targets[slot] = so_target
        for slot, entry in self.vertex_buffers.items():
            if entry is None:
                continue
            address, size, stride = entry
            found = self.d3d.at_address(address)
            if found is not None:
                resource, offset = found
                state.vertex_buffers[slot] = (resource.read()[offset:offset + size], stride, 0)
        state.topology = self.topology
        state.viewports = list(self.viewports)
        state.scissors = list(self.scissors)
        targets, srgb = [], set()
        for i, handle in enumerate(self.rtvs):
            descriptor = self.d3d.descriptor(handle)
            targets.append(self.d3d.view_image(descriptor) if descriptor is not None else None)
            if descriptor is not None and descriptor.values.get("format", 0) in DXGI_SRGB:
                srgb.add(i)
        state.targets, state.srgb = targets, srgb
        depth = self.d3d.descriptor(self.dsv) if self.dsv else None
        state.depth = self.d3d.view_image(depth) if depth is not None else None
        state.blends = pso.blends
        state.blend_factor = self.blend_factor
        state.depth_state = pso.depth_state
        fill, cull, front_ccw, depth_clip, _ = pso.rasterizer
        state.rasterizer = (fill, cull, front_ccw, depth_clip, bool(self.scissors))
        visibility = {"vs": 1, "hs": 2, "ds": 3, "gs": 4, "ps": 5, "as": 6, "ms": 7}
        state.uav_views = []
        for stage, shader in state.shaders.items():
            bindings, _ = self._bindings("graphics", shader, visibility[stage], state.uav_views)
            state.bindings[stage] = bindings
        return state

    def _draw_ids(self, vertex_ids, instances, start_instance):
        state = self._state()
        if state is None:
            return
        self.d3d.fragments += draw_dxbc(state, vertex_ids, [(i, start_instance + i) for i in range(instances)])
        self._write_back(state.uav_views)
        so = state.stream_out
        for target in (so.targets if so is not None else []):
            if target is None:
                continue
            if target.changed:
                target.owner.write(target.data[:target.owner.size])
            if target.filled is not None:   # the buffer's filled size, a UINT64 the GPU keeps
                holder, at = target.filled
                holder.write(struct.pack("<Q", target.offset - target.base), at)

    def _draw(self, vertices, instances, start, start_instance):
        self._draw_ids(numpy.arange(start, start + vertices), instances, start_instance)

    def _draw_indexed(self, count, instances, start, base, start_instance):
        if self.index_buffer is None:
            return
        address, size, fmt = self.index_buffer
        found = self.d3d.at_address(address)
        if found is None:
            return
        resource, offset = found
        width = 4 if fmt == 42 else 2
        data = resource.read()[offset:offset + size]
        indices = numpy.frombuffer(data.tobytes(), dtype="<u4" if width == 4 else "<u2").astype(numpy.int64)[start:start + count]
        base = base - (1 << 32) if base & 0x80000000 else base
        self._draw_ids(indices + base, instances, start_instance)

    def _program(self, program):
        self.program = program

    def _gpu_records(self, address, stride, count):
        found = self.d3d.at_address(address)
        if found is None:
            return []
        data = found[0].read()
        return [data[found[1] + k * stride:found[1] + (k + 1) * stride].tobytes() for k in range(count)]

    def _dispatch_graph(self, inputs, gpu):
        from .workgraph import Run
        if self.program is None:
            return
        state, graph = self.program
        if gpu is not None:         # D3D12_NODE_GPU_INPUT / D3D12_MULTI_NODE_GPU_INPUT in GPU memory
            mode, address = gpu
            found = self.d3d.at_address(address)
            if found is None:
                return
            raw = found[0].read()[found[1]:found[1] + 64].tobytes()
            if mode == 1:
                entry, count, start, stride = struct.unpack_from("<2I2Q", raw, 0)
                inputs = [(entry, self._gpu_records(start, stride, count))]
            else:
                count, _, start, stride = struct.unpack_from("<2I2Q", raw, 0)
                inputs = []
                for k in range(count):
                    item = self._gpu_records(start + k * stride, 24, 1)
                    if item:
                        entry, number, records, record_stride = struct.unpack_from("<2I2Q", item[0], 0)
                        inputs.append((entry, self._gpu_records(records, record_stride, number)))
        bindings, views = self._bindings("compute", state, 0)
        run = Run(graph, bindings)
        for entry, records in inputs:
            if 0 <= entry < len(graph.entrypoints):
                size = graph.nodes[graph.entrypoints[entry]].record_size
                run.enqueue(graph.entrypoints[entry], [r[:size] if size else r for r in records])
        run.run()
        self._write_back(views)

    def _dispatch_mesh(self, x, y, z):
        from .d3d_pipeline import draw_mesh
        state = self._state()
        if state is None:
            return
        self.d3d.fragments += draw_mesh(state, (x, y, z))
        self._write_back(state.uav_views)

    def _dispatch(self, x, y, z):
        pso = self.pso
        if pso is None or not pso.compute or "cs" not in pso.shaders:
            return
        shader = pso.shaders["cs"]
        bindings, views = self._bindings("compute", shader, 0)
        dispatch(shader, (x, y, z), bindings)
        self._write_back(views)

    # ray tracing (dxr.py) ---------------------------------------------------------
    def _build_structure(self, desc):
        structure = dxr.build(self.d3d, desc)
        if structure is not None:
            self.d3d.structures[desc["dest"]] = structure

    def _copy_structure(self, dst, src):
        if src in self.d3d.structures:
            self.d3d.structures[dst] = self.d3d.structures[src]

    def _state_object(self, state):
        self.state_object = state

    def _heaps(self, heaps):
        for heap in heaps:
            if heap is not None:
                self.heaps[heap.kind] = heap

    def _dispatch_rays(self, desc):
        state = self.state_object
        if state is None:
            return
        bindings, views = self._bindings("compute", state, 0)
        local = lambda root, data, base: self.local_bindings(root, data, state, base, views)    # noqa: E731
        dxr.Rays(self.d3d, state, desc, bindings, local).run()
        self._write_back(views)

    def _indirect(self, signature, count, arguments, offset):
        if signature is None or arguments is None or not signature.data:
            return
        stride, kinds = signature.data
        data = arguments.read()
        for i in range(count):
            at = offset + i * stride
            for kind in kinds:
                if kind == 0:
                    self._draw(*struct.unpack_from("<4I", data, at))
                    at += 16
                elif kind == 1:
                    self._draw_indexed(*struct.unpack_from("<5I", data, at))
                    at += 20
                elif kind == 2:
                    self._dispatch(*struct.unpack_from("<3I", data, at))
                    at += 12


class CommandQueue:
    interface = "ID3D12CommandQueue"
    flip_model = True

    def __init__(self, d3d, device, desc):
        self.d3d, self.device, self.desc = d3d, device, desc

    def swapchain_buffer(self, width, height, fmt):
        desc = struct.pack("<I4xQQIHHIIIII4x", 3, 0, width, height, 1, 1, fmt, 1, 0, 0, 1)
        return Resource12(self.d3d, desc, 1, 0)

    def ExecuteCommandLists(self, count, lists):
        dp = self.d3d.dp
        for i in range(count):
            command_list = self.d3d.com.impl(dp.read_ptr(lists + dp.ptr_size() * i))
            if command_list is not None:
                Executor(self.d3d).run(command_list.commands)

    def Signal(self, fence, value):
        impl = self.d3d.com.impl(fence)
        if impl is not None:
            impl.set(value)

    def Wait(self, fence, value):
        return 0

    def GetTimestampFrequency(self, out):
        self.d3d.dp.write(out, struct.pack("<Q", 1000000000))

    def GetClockCalibration(self, gpu, cpu):
        self.d3d.dp.write(gpu, struct.pack("<Q", 0))
        self.d3d.dp.write(cpu, struct.pack("<Q", 0))

    def GetDesc(self, out):
        self.d3d.dp.write(out, self.desc)
        return out

    def UpdateTileMappings(self, *args):
        missing(self.d3d.dp, "Direct3D 12 tiled resources")

    def CopyTileMappings(self, *args):
        missing(self.d3d.dp, "Direct3D 12 tiled resources")

    def SetMarker(self, metadata, data, size):
        return None

    def BeginEvent(self, metadata, data, size):
        return None

    def EndEvent(self):
        return None

    def GetDevice(self, riid, out):
        return self.d3d.device_query(riid, out)

    def SetName(self, name):
        return 0

    def SetPrivateData(self, guid, size, data):
        return 0

    def SetPrivateDataInterface(self, guid, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND


# ------------------------------------------------------------------ the device
class Device12:
    interface = "ID3D12Device"
    interfaces = [f"ID3D12Device{k}" for k in range(1, 15)]

    def __init__(self, d3d, level):
        self.d3d, self.level = d3d, level

    def _out(self, impl, interface, riid, out):
        if not out:
            return S_FALSE
        impl.interface = getattr(impl, "interface", None) or interface
        return self.d3d.dxgi.query(impl, riid, out, interface)

    def GetNodeCount(self):
        return 1

    def CreateCommandQueue(self, desc, riid, out):
        return self._out(CommandQueue(self.d3d, self, bytes(self.d3d.dp.read(desc, 16))), "ID3D12CommandQueue", riid, out)

    def CreateCommandAllocator(self, kind, riid, out):
        return self._out(Simple(self.d3d, "ID3D12CommandAllocator"), "ID3D12CommandAllocator", riid, out)

    def CreateCommandList(self, mask, kind, allocator, pso, riid, out):
        command_list = CommandList(self.d3d, kind, self.d3d.com.impl(pso) if pso else None)
        return self._out(command_list, "ID3D12GraphicsCommandList", riid, out)

    def CreateCommandList1(self, mask, kind, flags, riid, out):
        return self._out(CommandList(self.d3d, kind, None, closed=True), "ID3D12GraphicsCommandList", riid, out)

    def _shader(self, bytecode):
        address, size = bytecode.p, bytecode.size
        if not address or not size:
            return None
        return shader_of(bytes(self.d3d.dp.read(address, size)))

    def CreateGraphicsPipelineState(self, desc, riid, out):
        d = self.d3d.s.read("graphics", desc)
        pso = PipelineState(self.d3d)
        try:
            for stage, field in (("vs", d.vs), ("ps", d.ps), ("ds", d.ds), ("hs", d.hs), ("gs", d.gs)):
                shader = self._shader(field)
                if shader is not None:
                    if shader.error:
                        pso.error = shader.error
                    pso.shaders[stage] = shader
        except (dxbc.Unsupported, struct.error) as e:
            pso.error = str(e)
            missing(self.d3d.dp, f"Direct3D 12 shader: {e}")
        pso.root = self.d3d.com.impl(d.root) if d.root else None
        pso.layout = input_layout(self.d3d.dp, d.layout, d.layout_count) if d.layout and d.layout_count else []
        blend = struct.pack("<2I", d.alpha_to_coverage, d.independent)
        for rt in d.rt:
            blend += struct.pack("<9IB3x", rt.enable, 0, rt.src, rt.dst, rt.op, rt.src_a, rt.dst_a, rt.op_a, 0, rt.mask)
        from .d3d11_emu import State11
        pso.blends = blends_of(State11(self.d3d, "", blend))
        pso.rasterizer = (d.fill, d.cull, bool(d.front_ccw), bool(d.depth_clip), True)
        pso.depth_state = (bool(d.depth_enable), bool(d.depth_write), d.depth_func)
        if d.so_decl and d.so_entries:
            from .d3d11_emu import so_entries
            dp = self.d3d.dp
            strides = [struct.unpack("<I", bytes(dp.read(d.so_strides + 4 * i, 4)))[0] for i in range(d.so_num_strides)] \
                if d.so_strides else []
            pso.so = (so_entries(dp, d.so_decl, d.so_entries, True), strides,
                      -1 if d.so_stream == 0xFFFFFFFF else d.so_stream)
        return self._out(pso, "ID3D12PipelineState", riid, out)

    def CreateComputePipelineState(self, desc, riid, out):
        d = self.d3d.s.read("compute", desc)
        pso = PipelineState(self.d3d)
        pso.compute = True
        try:
            shader = self._shader(d.cs)
            if shader is not None:
                pso.shaders["cs"] = shader
                if shader.error:
                    pso.error = shader.error
                    missing(self.d3d.dp, f"Direct3D 12 compute shader: {shader.error}")
        except (dxbc.Unsupported, struct.error) as e:
            pso.error = str(e)
            missing(self.d3d.dp, f"Direct3D 12 shader: {e}")
        pso.root = self.d3d.com.impl(d.root) if d.root else None
        return self._out(pso, "ID3D12PipelineState", riid, out)

    # the subobjects of a pipeline state stream: type -> (alignment, size) of their value (p: the pointer size)
    STREAM = {0: ("p", "p"), 1: ("p", "2p"), 2: ("p", "2p"), 3: ("p", "2p"), 4: ("p", "2p"), 5: ("p", "2p"), 6: ("p", "2p"),
              7: ("p", "so"), 8: (4, 328), 9: (4, 4), 10: (4, 44), 11: (4, 52), 12: ("p", "2p"), 13: (4, 4), 14: (4, 4),
              15: (4, 36), 16: (4, 4), 17: (4, 8), 18: (4, 4), 19: ("p", "2p"), 20: (4, 4), 21: (4, 56), 22: ("p", "vi"),
              24: ("p", "2p"), 25: ("p", "2p"), 26: (4, 60), 27: (4, 44), 28: (4, 40), 29: ("p", "2p")}

    def CreatePipelineState(self, desc, riid, out):
        """ID3D12Device2::CreatePipelineState: a stream of subobjects (graphics, compute or mesh pipelines)."""
        dp = self.d3d.dp
        p = dp.ptr_size()
        size = dp.read_ptr(desc)
        stream = dp.read_ptr(desc + p)
        data = bytes(dp.read(stream, size)) if size else b""
        fields = {}
        offset = 0
        while offset + 4 <= len(data):
            kind = struct.unpack_from("<I", data, offset)[0]
            layout = self.STREAM.get(kind)
            if layout is None:
                missing(dp, f"Direct3D 12 pipeline state subobject {kind}")
                return E_INVALIDARG
            align = p if layout[0] == "p" else layout[0]
            length = {"p": p, "2p": 2 * p, "so": 32 if p == 8 else 20, "vi": 24 if p == 8 else 12}.get(layout[1], layout[1])
            start = offset + 4 + (-(offset + 4) % align)
            fields[kind] = data[start:start + length]
            offset = start + length
            offset += -offset % p
        pso = PipelineState(self.d3d)

        def pointer(raw):
            return struct.unpack_from("<Q" if p == 8 else "<I", raw, 0)[0]

        def bytecode(raw):
            return pointer(raw), struct.unpack_from("<Q" if p == 8 else "<I", raw, p)[0]
        try:
            for kind, stage in ((1, "vs"), (2, "ps"), (3, "ds"), (4, "hs"), (5, "gs"), (6, "cs"), (24, "as"), (25, "ms")):
                if kind in fields:
                    address, length = bytecode(fields[kind])
                    if address and length:
                        shader = shader_of(bytes(dp.read(address, length)))
                        if shader.error:
                            pso.error = shader.error
                        pso.shaders[stage] = shader
        except (dxbc.Unsupported, struct.error, BitcodeError) as e:
            pso.error = str(e)
            missing(dp, f"Direct3D 12 shader: {e}")
        pso.compute = "cs" in pso.shaders
        if 0 in fields:
            pso.root = self.d3d.com.impl(pointer(fields[0]))
        if 12 in fields:
            address, count = pointer(fields[12]), struct.unpack_from("<I", fields[12], p)[0]
            pso.layout = input_layout(dp, address, count) if address and count else []
        blend = fields.get(8) or (struct.pack("<2I", 0, 0) + struct.pack("<9IB3x", 0, 0, 2, 1, 1, 2, 1, 1, 4, 15) * 8)
        from .d3d11_emu import State11
        pso.blends = blends_of(State11(self.d3d, "", blend))
        raster = next((fields[k] for k in (28, 27, 10) if k in fields), None)
        if raster is not None:
            fill, cull, front_ccw = struct.unpack_from("<3I", raster, 0)
            depth_clip = struct.unpack_from("<I", raster, 24)[0]
            pso.rasterizer = (fill, cull, bool(front_ccw), bool(depth_clip), True)
        else:
            pso.rasterizer = (3, 3, False, True, True)
        depth = next((fields[k] for k in (26, 21, 11) if k in fields), None)
        if depth is not None:
            enable, write, function = struct.unpack_from("<3I", depth, 0)
            pso.depth_state = (bool(enable), bool(write), function)
        else:
            pso.depth_state = (True, True, 2)
        if 7 in fields:
            from .d3d11_emu import so_entries
            raw = fields[7]
            if p == 8:
                decl, entries, strides_at, num_strides, stream_index = struct.unpack_from("<QI4xQII", raw, 0)
            else:
                decl, entries, strides_at, num_strides, stream_index = struct.unpack_from("<5I", raw, 0)
            if decl and entries:
                strides = [struct.unpack("<I", bytes(dp.read(strides_at + 4 * i, 4)))[0] for i in range(num_strides)] \
                    if strides_at else []
                pso.so = (so_entries(dp, decl, entries, True), strides, -1 if stream_index == 0xFFFFFFFF else stream_index)
        return self._out(pso, "ID3D12PipelineState", riid, out)

    def CreateDescriptorHeap(self, desc, riid, out):
        kind, count, flags, _ = struct.unpack("<4I", bytes(self.d3d.dp.read(desc, 16)))
        return self._out(DescriptorHeap(self.d3d, kind, count, flags), "ID3D12DescriptorHeap", riid, out)

    def GetDescriptorHandleIncrementSize(self, kind):
        return INCREMENT

    def CreateRootSignature(self, mask, blob, size, riid, out):
        root = RootSignature(self.d3d, bytes(self.d3d.dp.read(blob, size)))
        return self._out(root, "ID3D12RootSignature", riid, out)

    def _store(self, handle, descriptor):
        heap, index = self.d3d.cpu_heap(handle)
        if heap is not None and 0 <= index < heap.count:
            heap.descriptors[index] = descriptor

    def CreateConstantBufferView(self, desc, handle):
        location, size = struct.unpack("<QI", bytes(self.d3d.dp.read(desc, 12))) if desc else (0, 0)
        self._store(handle, Descriptor("cbv", None, location=location, size=size))

    def CreateShaderResourceView(self, resource, desc, handle):
        impl = self.d3d.com.impl(resource)
        values = {}
        if desc:
            d = self.d3d.s.read("srv", desc)
            values = dict(format=d.format, dimension=d.dimension, first=int(d.first), count=d.count, stride=d.stride,
                          flags=d.flags)
            if d.dimension in (4, 8, 9):    # most detailed mip, mip levels
                raw = bytes(self.d3d.dp.read(desc + 16, 8))
                values["mip"], values["mips"] = struct.unpack("<2I", raw)
            elif d.dimension in (5, 3):
                raw = bytes(self.d3d.dp.read(desc + 16, 16))
                values["mip"], values["mips"], values["first_slice"], values["slices"] = struct.unpack("<4I", raw)
            elif d.dimension == 11:         # RAYTRACING_ACCELERATION_STRUCTURE: its GPU address
                values["location"] = struct.unpack("<Q", bytes(self.d3d.dp.read(desc + 16, 8)))[0]
        self._store(handle, Descriptor("srv", impl, **values))

    def CreateUnorderedAccessView(self, resource, counter, desc, handle):
        impl = self.d3d.com.impl(resource)
        values = {}
        if desc:
            d = self.d3d.s.read("uav", desc)
            values = dict(format=d.format, dimension=d.dimension, first=int(d.first), count=d.count, stride=d.stride,
                          flags=d.flags)
            if d.dimension in (4, 2):
                values["mip"] = struct.unpack("<I", bytes(self.d3d.dp.read(desc + 8, 4)))[0]
            elif d.dimension == 1 and counter:
                values["counter"] = (self.d3d.com.impl(counter), int(d.counter_offset))
        self._store(handle, Descriptor("uav", impl, **values))

    def CreateRenderTargetView(self, resource, desc, handle):
        impl = self.d3d.com.impl(resource)
        values = {}
        if desc:
            d = self.d3d.s.read("rtv", desc)
            values = dict(format=d.format, dimension=d.dimension)
            if d.dimension == 4:
                values["mip"] = int(d.a) & 0xFFFFFFFF
            elif d.dimension == 5:
                values["mip"], values["first_slice"] = int(d.a) & 0xFFFFFFFF, int(d.a) >> 32
        self._store(handle, Descriptor("rtv", impl, **values))

    def CreateDepthStencilView(self, resource, desc, handle):
        impl = self.d3d.com.impl(resource)
        values = {}
        if desc:
            d = self.d3d.s.read("dsv", desc)
            values = dict(format=d.format, dimension=d.dimension, mip=d.a)
        self._store(handle, Descriptor("dsv", impl, **values))

    def CreateSampler(self, desc, handle):
        sampler = Sampler.from_d3d11(bytes(self.d3d.dp.read(desc, 52)))
        self._store(handle, Descriptor("sampler", None, sampler=sampler))

    def CopyDescriptors(self, dst_count, dst_starts, dst_sizes, src_count, src_starts, src_sizes, kind):
        dp = self.d3d.dp
        ptr = dp.ptr_size()
        sources = []
        for i in range(src_count):
            start = dp.read_ptr(src_starts + ptr * i)
            size = struct.unpack("<I", bytes(dp.read(src_sizes + 4 * i, 4)))[0] if src_sizes else 1
            sources += [start + INCREMENT * k for k in range(size)]
        targets = []
        for i in range(dst_count):
            start = dp.read_ptr(dst_starts + ptr * i)
            size = struct.unpack("<I", bytes(dp.read(dst_sizes + 4 * i, 4)))[0] if dst_sizes else 1
            targets += [start + INCREMENT * k for k in range(size)]
        for target, source in zip(targets, sources):
            self._store(target, self.d3d.descriptor(source))

    def CopyDescriptorsSimple(self, count, dst, src, kind):
        for k in range(count):
            self._store(dst + INCREMENT * k, self.d3d.descriptor(src + INCREMENT * k))

    def GetResourceAllocationInfo(self, out, mask, count, descs):
        size = 0
        desc_size = self.d3d.s.size("resource_desc")
        for i in range(count):
            d = self.d3d.s.read("resource_desc", descs + desc_size * i)
            kind = DXGI.get(d.format, "rgba8")
            size += int(d.width) if d.dimension == 1 else pitch_of(kind, int(d.width)) * max(1, d.height) * max(1, d.depth) * 2
        self.d3d.dp.write(out, struct.pack("<2Q", (size + 65535) // 65536 * 65536, 65536))
        return out

    def GetResourceAllocationInfo1(self, out, mask, count, descs, infos):
        return self.GetResourceAllocationInfo(out, mask, count, descs)

    def GetCustomHeapProperties(self, out, mask, kind):
        self.d3d.dp.write(out, struct.pack("<5I", 0, 1 if kind == 1 else 3, 1, 1, 1))
        return out

    def _resource(self, props, desc, state, riid, out):
        heap_type = struct.unpack("<I", bytes(self.d3d.dp.read(props, 4)))[0] if props else 1
        resource = Resource12(self.d3d, bytes(self.d3d.dp.read(desc, self.d3d.s.size("resource_desc"))), heap_type, state)
        return self._out(resource, "ID3D12Resource", riid, out)

    def CreateCommittedResource(self, props, flags, desc, state, clear, riid, out):
        return self._resource(props, desc, state, riid, out)

    def CreateCommittedResource1(self, props, flags, desc, state, clear, session, riid, out):
        return self._resource(props, desc, state, riid, out)

    def CreateHeap(self, desc, riid, out):
        heap = Simple(self.d3d, "ID3D12Heap", bytes(self.d3d.dp.read(desc, 40)))
        return self._out(heap, "ID3D12Heap", riid, out)

    def CreateHeap1(self, desc, session, riid, out):
        return self.CreateHeap(desc, riid, out)

    def CreatePlacedResource(self, heap, offset, desc, state, clear, riid, out):
        impl = self.d3d.com.impl(heap)
        heap_type = struct.unpack_from("<I", impl.data, 8)[0] if impl is not None and impl.data else 1
        resource = Resource12(self.d3d, bytes(self.d3d.dp.read(desc, self.d3d.s.size("resource_desc"))), heap_type, state)
        return self._out(resource, "ID3D12Resource", riid, out)

    def CreateReservedResource(self, desc, state, clear, riid, out):
        return self._resource(0, desc, state, riid, out)

    def CreateReservedResource1(self, desc, state, clear, session, riid, out):
        return self._resource(0, desc, state, riid, out)

    def CreateSharedHandle(self, *args):
        return E_NOTIMPL

    def OpenSharedHandle(self, *args):
        return E_NOTIMPL

    def OpenSharedHandleByName(self, *args):
        return E_NOTIMPL

    def MakeResident(self, count, objects):
        return 0

    def Evict(self, count, objects):
        return 0

    def EnqueueMakeResident(self, flags, count, objects, fence, value):
        impl = self.d3d.com.impl(fence)
        if impl is not None:
            impl.set(value)

    def CreateFence(self, value, flags, riid, out):
        return self._out(Fence(self.d3d, value), "ID3D12Fence", riid, out)

    def GetDeviceRemovedReason(self):
        return 0

    def GetCopyableFootprints(self, desc, first, count, base, layouts, rows_out, sizes_out, total_out):
        d = self.d3d.s.read("resource_desc", desc)
        dp = self.d3d.dp
        offset = base
        mips = d.mips or 1
        kind = DXGI.get(d.format, "rgba8")
        for i in range(first, first + count):
            if d.dimension == 1:
                width, height, depth, pitch, rows = int(d.width), 1, 1, int(d.width), 1
            else:
                mip = i % mips
                width, height = max(1, int(d.width) >> mip), max(1, d.height >> mip)
                depth = max(1, d.depth >> mip) if d.dimension == 4 else 1
                pitch = (pitch_of(kind, width) + 255) // 256 * 256
                rows = rows_of(kind, height)
            offset = (offset + 511) // 512 * 512 if d.dimension != 1 else offset
            k = i - first
            if layouts:
                dp.write(layouts + 32 * k, struct.pack("<Q5I4x", offset, d.format, width, height, depth, pitch))
            if rows_out:
                dp.write(rows_out + 4 * k, struct.pack("<I", rows))
            if sizes_out:
                dp.write(sizes_out + 8 * k, struct.pack("<Q", pitch_of(kind, width) if d.dimension != 1 else width))
            offset += pitch * rows * depth
        if total_out:
            dp.write(total_out, struct.pack("<Q", offset - base))

    def CreateQueryHeap(self, desc, riid, out):
        return self._out(Simple(self.d3d, "ID3D12QueryHeap", bytes(self.d3d.dp.read(desc, 12))), "ID3D12QueryHeap", riid, out)

    def SetStablePowerState(self, enable):
        return 0

    def CreateCommandSignature(self, desc, root, riid, out):
        dp = self.d3d.dp
        stride, count = struct.unpack("<2I", bytes(dp.read(desc, 8)))
        arguments = dp.read_ptr(desc + 8)
        kinds = [struct.unpack("<I", bytes(dp.read(arguments + 16 * i, 4)))[0] for i in range(count)]
        return self._out(Simple(self.d3d, "ID3D12CommandSignature", (stride, kinds)), "ID3D12CommandSignature", riid, out)

    def GetResourceTiling(self, *args):
        return None

    def GetAdapterLuid(self, out):
        self.d3d.dp.write(out, struct.pack("<Ii", 0x46544D, 0))
        return out

    def CheckFeatureSupport(self, feature, data, size):
        dp = self.d3d.dp
        if feature == 2 and size >= 12:            # FEATURE_LEVELS: the highest requested we accept
            count = struct.unpack("<I", bytes(dp.read(data, 4)))[0]
            levels = dp.read_ptr(data + dp.ptr_size())
            requested = struct.unpack(f"<{count}I", bytes(dp.read(levels, 4 * count))) if count else ()
            best = max([lvl for lvl in requested if lvl <= 0xC100] or [0xB000])
            dp.write(data + 2 * dp.ptr_size(), struct.pack("<I", best))
            return 0
        if feature == 7 and size >= 4:              # SHADER_MODEL: up to 6.6 (DXIL, dxil.py)
            requested = struct.unpack("<I", bytes(dp.read(data, 4)))[0]
            dp.write(data, struct.pack("<I", min(requested, 0x69) if requested >= 0x51 else 0x51))
            return 0
        if feature == 12 and size >= 4:             # ROOT_SIGNATURE: 1.1
            dp.write(data, struct.pack("<I", 2))
            return 0
        if feature == 1 and size >= 16:             # ARCHITECTURE: UMA
            dp.write(data + 4, struct.pack("<3I", 0, 1, 1))
            return 0
        if feature == 3 and size >= 12:             # FORMAT_SUPPORT
            dp.write(data + 4, struct.pack("<2I", 0xFFFFFFFF, 0xFFFFFFFF))
            return 0
        if feature == 4 and size >= 16:             # MULTISAMPLE_QUALITY_LEVELS
            count = struct.unpack("<I", bytes(dp.read(data + 4, 4)))[0]
            dp.write(data + 12, struct.pack("<I", 1 if count == 1 else 0))
            return 0
        if feature == 5 and size >= 8:              # FORMAT_INFO: planes
            dp.write(data + 4, struct.pack("<B", 1))
            return 0
        dp.write(data, b"\0" * size)
        if feature == 27 and size >= 12:            # OPTIONS5: ray tracing tier 1.1 (dxr.py, RayQuery)
            dp.write(data + 8, struct.pack("<I", 11))
        if feature == 32 and size >= 8:             # OPTIONS7: mesh shaders tier 1 (d3d_pipeline.draw_mesh)
            dp.write(data, struct.pack("<I", 10))
        if feature == 53 and size >= 8:             # OPTIONS21: work graphs tier 1.0 (workgraph.py)
            dp.write(data, struct.pack("<I", 0x10))
        return 0

    def CreatePipelineLibrary(self, *args):
        return E_NOTIMPL

    def SetEventOnMultipleFenceCompletion(self, fences, values, count, flags, event):
        self.d3d.set_event(event)

    def SetResidencyPriority(self, count, objects, priorities):
        return 0

    def OpenExistingHeapFromAddress(self, *args):
        return E_NOTIMPL

    def OpenExistingHeapFromFileMapping(self, *args):
        return E_NOTIMPL

    def CreateProtectedResourceSession(self, *args):
        return E_NOTIMPL

    def CreateLifetimeTracker(self, *args):
        return E_NOTIMPL

    def RemoveDevice(self):
        return None

    def EnumerateMetaCommands(self, count, descs):
        self.d3d.dp.write(count, struct.pack("<I", 0))

    def CreateStateObject(self, desc, riid, out):
        try:
            state = dxr.create_state_object(self.d3d, desc)
        except (dxbc.Unsupported, BitcodeError, KeyError) as e:
            missing(self.d3d.dp, f"Direct3D 12 state object: {e}")
            return E_FAIL
        return self._out(state, "ID3D12StateObject", riid, out)

    def AddToStateObject(self, desc, existing, riid, out):
        base = self.d3d.com.impl(existing)
        try:
            state = dxr.create_state_object(self.d3d, desc, base if isinstance(base, dxr.StateObject) else None)
        except (dxbc.Unsupported, BitcodeError, KeyError) as e:
            missing(self.d3d.dp, f"Direct3D 12 state object: {e}")
            return E_FAIL
        return self._out(state, "ID3D12StateObject", riid, out)

    def GetRaytracingAccelerationStructurePrebuildInfo(self, desc, info):
        sizes = dxr.prebuild_sizes(dxr.read_inputs(self.d3d.dp, desc))
        self.d3d.dp.write(info, struct.pack("<3Q", *sizes))

    def CheckDriverMatchingIdentifier(self, *args):
        return 0

    def SetName(self, name):
        return 0

    def SetPrivateData(self, guid, size, data):
        return 0

    def SetPrivateDataInterface(self, guid, data):
        return 0

    def GetPrivateData(self, guid, size, data):
        return DXGI_ERROR_NOT_FOUND


class Direct3D12:
    def __init__(self, dp):
        self.dp = dp
        self.dxgi = dxgi_of(dp)
        self.com = self.dxgi.com
        self.s = Struct(dp)
        self.devices: List[Device12] = []
        self.heaps: List[DescriptorHeap] = []
        self.buffers: List[Resource12] = []
        self.structures: Dict[int, object] = {}     # acceleration structures by their GPU address (dxr.py)
        self.programs: Dict[bytes, tuple] = {}      # work graph program identifiers -> (state object, graph)
        self.fragments = 0

    def device_query(self, riid, out):
        return self.dxgi.query(self.devices[-1], riid, out) if self.devices else E_FAIL

    def set_event(self, handle):
        if not handle:
            return
        try:
            from .fathom_nt import _event_set
            _event_set(self.dp, handle, 0, True)
        except Exception:
            pass

    # descriptors ------------------------------------------------------------
    def cpu_heap(self, handle):
        for heap in self.heaps:
            if heap.cpu <= handle < heap.cpu + heap.count * INCREMENT:
                return heap, (handle - heap.cpu) // INCREMENT
        return None, 0

    def gpu_heap(self, handle):
        for heap in self.heaps:
            if heap.gpu <= handle < heap.gpu + heap.count * INCREMENT:
                return heap, (handle - heap.gpu) // INCREMENT
        return None, 0

    def descriptor(self, handle) -> Optional[Descriptor]:
        heap, index = self.cpu_heap(handle)
        if heap is None:
            heap, index = self.gpu_heap(handle)
        return heap.descriptors[index] if heap is not None and 0 <= index < heap.count else None

    def at_address(self, address):
        """The buffer that holds a GPU virtual address, and the offset in it."""
        for resource in reversed(self.buffers):
            if resource.address <= address < resource.address + max(1, resource.size):
                return resource, address - resource.address
        return None

    def view_image(self, descriptor: Descriptor) -> Optional[Image]:
        resource = descriptor.resource
        if resource is None or not resource.images:
            return None
        s = min(descriptor.values.get("first_slice", 0), len(resource.images) - 1)
        levels = resource.images[s]
        return levels[min(descriptor.values.get("mip", 0), len(levels) - 1)]

    def gpu_view(self, descriptor: Descriptor) -> Optional[View]:
        resource = descriptor.resource
        values = descriptor.values
        if values.get("dimension") == 11 and descriptor.kind == "srv":
            view = View("raw")
            view.address = values.get("location", 0)
            view.structures = self.structures
            return view
        if resource is None:
            return None
        if resource.dimension == "buffer":
            stride = values.get("stride", 0)
            raw = bool(values.get("flags", 0) & 1)
            fmt = values.get("format", 0)
            if raw:
                view = View("raw", count=values.get("count", 0), first=values.get("first", 0))
            elif stride:
                view = View("structured", stride=stride, count=values.get("count", 0), first=values.get("first", 0))
            else:
                view = View("buffer", fmt=DXGI.get(fmt, "r32u"), count=values.get("count", 0), first=values.get("first", 0))
            view.bytes = resource.read()
            view.source = descriptor
            counter = values.get("counter")
            if counter is not None and counter[0] is not None:     # the hidden counter of an append / consume UAV
                holder, at = counter
                view.counter = [int(holder.read()[at:at + 4].view("<u4")[0])]
                view.counter_source = counter
            return view
        dimension = {1: "buffer", 2: "1d", 3: "1darray", 4: "2d", 5: "2darray", 6: "2d", 7: "2darray", 8: "3d", 9: "cube",
                     10: "cube"}.get(values.get("dimension", 4), "2d")
        first = values.get("first_slice", 0)
        slices = values.get("slices", len(resource.images)) or len(resource.images)
        if dimension in ("2d", "1d", "3d"):
            first, slices = 0, len(resource.images)
            if dimension != "3d":
                slices = 1
        images = resource.images[first:first + slices]
        view = View("texture", images=images, dimension=dimension if len(images) > 1 or dimension == "cube" else
                    ("2d" if dimension == "2darray" else dimension), mip=values.get("mip", 0),
                    mips=max(1, values.get("mips", resource.mips) if values.get("mips", 0) != 0xFFFFFFFF else resource.mips))
        view.srgb = values.get("format", resource.format) in DXGI_SRGB
        return view

    def location(self, address):
        """A D3D12_TEXTURE_COPY_LOCATION: (resource, type, subresource or footprint)."""
        dp = self.dp
        resource = self.com.impl(dp.read_ptr(address))
        x64 = dp._x64
        kind = struct.unpack("<I", bytes(dp.read(address + (8 if x64 else 4), 4)))[0]
        union = address + (16 if x64 else 8)
        if kind == 0:
            return resource, 0, struct.unpack("<I", bytes(dp.read(union, 4)))[0]
        offset, fmt, width, height, depth, pitch = struct.unpack("<Q5I", bytes(dp.read(union, 28)))
        return resource, 1, (offset, fmt, width, height, depth, pitch)

    # exports ----------------------------------------------------------------
    def create_device(self, dp, adapter, level, riid, out):
        if level > 0xC100:
            return E_INVALIDARG
        device = Device12(self, level)
        self.devices.append(device)
        self.dxgi.events.append(dict(kind="direct3d12_device", level=hex(level)))
        if not out:
            return S_FALSE
        return self.dxgi.query(device, riid, out, "ID3D12Device")

    def serialize(self, dp, desc, version, blob_out, error_out):
        if error_out:
            dp.write_ptr(error_out, 0)
        data = serialize_root_signature(dp, desc, 1 if version <= 1 else 2, self.s)
        dp.write_ptr(blob_out, self.com.create(Blob(self, data), "ID3D10Blob"))
        return 0

    def serialize_versioned(self, dp, desc, blob_out, error_out):
        version = struct.unpack("<I", bytes(dp.read(desc, 4)))[0]
        return self.serialize(dp, desc + (8 if dp._x64 else 4), version, blob_out, error_out)


def install(dp):
    if numpy is None:
        return None
    d3d = Direct3D12(dp)
    dp.direct3d12 = d3d
    hooks = dp.apihooks
    hooks.register_signature("d3d12.dll", "D3D12CreateDevice", "pipp", d3d.create_device)
    hooks.register_signature("d3d12.dll", "D3D12GetDebugInterface", "pp", lambda dp, riid, out: (dp.write_ptr(out, 0), E_NOINTERFACE)[1])
    hooks.register_signature("d3d12.dll", "D3D12SerializeRootSignature", "pipp", d3d.serialize)
    hooks.register_signature("d3d12.dll", "D3D12SerializeVersionedRootSignature", "ppp", d3d.serialize_versioned)
    hooks.register_signature("d3d12.dll", "D3D12EnableExperimentalFeatures", "ippp", lambda dp, *args: E_NOINTERFACE)
    hooks.register_signature("d3d12.dll", "D3D12GetInterface", "ppp", lambda dp, *args: E_NOINTERFACE)
    hooks.register_signature("d3d12.dll", "D3D12CreateRootSignatureDeserializer", "pppp", lambda dp, *args: E_NOTIMPL)
    hooks.register_signature("d3d12.dll", "D3D12CreateVersionedRootSignatureDeserializer", "pppp", lambda dp, *args: E_NOTIMPL)
    return d3d
