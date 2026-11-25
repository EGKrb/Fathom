# Fathom addition to dumpulator: Direct3D 10 and 10.1 for the emulated program, on the objects of the
# Direct3D 11 of d3d11_emu.py (the device of Direct3D 10 is also its context). Boost Software License 1.0,
# like the rest of dumpulator (LICENSE.dumpulator).
#
# What differs from Direct3D 11 is translated: shaders set with one argument, integer viewports, the blend
# description (D3D10_BLEND_DESC), the depth stencil view description, Map / Unmap on the resources, the
# 20-byte buffer description. The effects (D3D10CreateEffectFromMemory) are d3d10_effect.py.

import struct

from . import dxbc
from .d3d11_emu import (Context11, Device11, Direct3D11, Resource11, State11, View11, input_layout, so_entries, E_INVALIDARG,
                        BIND_RENDER_TARGET)
from .dxgi_emu import DEVICES, missing, S_FALSE
from .d3d10_effect import create_effect
from .gpu import rows_of
from .raster import numpy

E_FAIL = 0x80004005


class Resource10(Resource11):
    """A resource of Direct3D 10: Map / Unmap on the resource itself."""

    def __init__(self, d3d, dimension, desc, interface):
        if dimension == "buffer":
            desc = bytes(desc[:20]) + b"\0\0\0\0"
        super().__init__(d3d, dimension, desc, interface)
        self.interfaces = ["IDXGISurface", "IDXGISurface1", "IDXGIResource"] if dimension == "2d" else ["IDXGIResource"]

    def GetDesc(self, out):
        self.d3d.dp.write(out, bytes(self.desc[:20]) if self.dimension == "buffer" else bytes(self.desc))

    def Map(self, *args):
        dp = self.d3d.dp
        if self.dimension == "buffer":      # Map(type, flags, void **data)
            kind, flags, out = args
            dp.write_ptr(out, self.address)
            return 0
        if len(args) == 2:                  # IDXGISurface::Map(locked, flags)
            return super().Map(*args)
        sub, kind, flags, mapped = args
        image = self.subresource(sub)
        if image is None or not mapped:
            return E_INVALIDARG
        address = image.map(dp)
        if self.dimension == "1d":
            dp.write_ptr(mapped, address)
        else:
            dp.write_ptr(mapped, address)
            pitch = image.pitch
            extra = struct.pack("<I", pitch) if self.dimension == "2d" else \
                struct.pack("<2I", pitch, pitch * rows_of(image.kind, image.height))
            dp.write(mapped + dp.ptr_size(), extra)
        return 0

    def Unmap(self, *args):
        if self.dimension == "buffer":
            return None
        if not args:
            return super().Unmap()
        image = self.subresource(args[0])
        if image is not None:
            image.unmap(self.d3d.dp)


class View10(View11):
    def _parse(self, desc: bytes):
        if self.kind == "dsv":  # no Flags: the union right after the dimension
            desc = desc[:8] + b"\0\0\0\0" + desc[8:]
        super()._parse(desc)


def blend10_to_11(data: bytes) -> bytes:
    """D3D10_BLEND_DESC -> D3D11_BLEND_DESC (independent: the enables and masks per target)."""
    coverage = struct.unpack_from("<I", data, 0)[0]
    enables = struct.unpack_from("<8I", data, 4)
    src, dst, op, src_a, dst_a, op_a = struct.unpack_from("<6I", data, 36)
    masks = data[60:68]
    out = struct.pack("<2I", coverage, 1)
    for i in range(8):
        out += struct.pack("<7I", enables[i], src, dst, op, src_a, dst_a, op_a) + bytes([masks[i]]) + b"\0\0\0"
    return out


class Device10(Context11, Device11):
    interface = "ID3D10Device"
    interfaces = ["ID3D10Device1", "ID3D10Multithread"] + DEVICES

    def __init__(self, d3d, level, flags):
        self.d3d, self.dxgi, self.level, self.flags = d3d, d3d.dxgi, level, flags
        self.device = self
        Context11.ClearState(self)

    def swapchain_buffer(self, width, height, fmt):
        desc = struct.pack("<11I", width, height, 1, 1, fmt, 1, 0, 0, BIND_RENDER_TARGET, 0, 0)
        return Resource10(self.d3d, "2d", desc, "ID3D10Texture2D")

    # shaders: one argument
    def VSSetShader(self, shader):
        self._shader("vs", shader)

    def PSSetShader(self, shader):
        self._shader("ps", shader)

    def GSSetShader(self, shader):
        self._shader("gs", shader)

    def VSGetShader(self, out):
        self._get_shader("vs", out, 0, 0)

    def PSGetShader(self, out):
        self._get_shader("ps", out, 0, 0)

    def GSGetShader(self, out):
        self._get_shader("gs", out, 0, 0)

    def RSSetViewports(self, count, viewports):
        dp = self.d3d.dp
        self.viewports = []
        for i in range(count):
            x, y, w, h, near, far = struct.unpack("<2i2I2f", bytes(dp.read(viewports + 24 * i, 24)))
            self.viewports.append((float(x), float(y), float(w), float(h), near, far))

    def RSGetViewports(self, count, viewports):
        dp = self.d3d.dp
        if not viewports:
            dp.write(count, struct.pack("<I", len(self.viewports)))
            return
        n = min(struct.unpack("<I", bytes(dp.read(count, 4)))[0], len(self.viewports))
        for i in range(n):
            x, y, w, h, near, far = self.viewports[i]
            dp.write(viewports + 24 * i, struct.pack("<2i2I2f", int(x), int(y), int(w), int(h), near, far))
        dp.write(count, struct.pack("<I", n))

    def OMSetBlendState(self, state, factor, mask):
        return Context11.OMSetBlendState(self, state, factor, mask)

    # creation
    def _resource(self, dimension, desc, size, initial, out, interface):
        resource = Resource10(self.d3d, dimension, bytes(self.d3d.dp.read(desc, size)), interface)
        resource.initial(initial)
        return self.dxgi.out(resource, interface, out)

    def CreateBuffer(self, desc, initial, out):
        return self._resource("buffer", desc, 20, initial, out, "ID3D10Buffer")

    def CreateTexture1D(self, desc, initial, out):
        return self._resource("1d", desc, 32, initial, out, "ID3D10Texture1D")

    def CreateTexture2D(self, desc, initial, out):
        return self._resource("2d", desc, 44, initial, out, "ID3D10Texture2D")

    def CreateTexture3D(self, desc, initial, out):
        return self._resource("3d", desc, 36, initial, out, "ID3D10Texture3D")

    def _view10(self, resource, desc, out, kind, interface, size):
        impl = self.d3d.com.impl(resource)
        if not isinstance(impl, Resource11):
            return E_INVALIDARG
        view = View10(self.d3d, impl, kind, bytes(self.d3d.dp.read(desc, size)) if desc else None, interface)
        return self.dxgi.out(view, interface, out)

    def CreateRenderTargetView(self, resource, desc, out):
        return self._view10(resource, desc, out, "rtv", "ID3D10RenderTargetView", 20)

    def CreateDepthStencilView(self, resource, desc, out):
        return self._view10(resource, desc, out, "dsv", "ID3D10DepthStencilView", 20)

    def CreateShaderResourceView(self, resource, desc, out):
        return self._view10(resource, desc, out, "srv", "ID3D10ShaderResourceView", 24)

    def CreateShaderResourceView1(self, resource, desc, out):
        return self._view10(resource, desc, out, "srv", "ID3D10ShaderResourceView1", 24)

    def CreateInputLayout(self, descs, count, bytecode, length, out):
        return self.dxgi.out(State11(self.d3d, "ID3D10InputLayout", input_layout(self.d3d.dp, descs, count)),
                             "ID3D10InputLayout", out)

    def CreateVertexShader(self, bytecode, length, out):
        return self._shader_object(bytecode, length, out, "ID3D10VertexShader")

    def CreatePixelShader(self, bytecode, length, out):
        return self._shader_object(bytecode, length, out, "ID3D10PixelShader")

    def CreateGeometryShader(self, bytecode, length, out):
        return self._shader_object(bytecode, length, out, "ID3D10GeometryShader")

    def CreateGeometryShaderWithStreamOutput(self, bytecode, length, decl, entries, stride, out):
        result = self._shader_object(bytecode, length, out, "ID3D10GeometryShader")
        state = self.d3d.com.impl(self.d3d.dp.read_ptr(out)) if out and result == 0 else None
        if state is not None:
            items = so_entries(self.d3d.dp, decl, entries, False)
            slots = {item[5] for item in items}
            # one buffer: the given stride; several (each a single element): their components
            strides = [stride] if len(slots) <= 1 else []
            state.so = (items, strides, 0)
        return result

    def SOGetTargets(self, count, buffers, offsets):
        Context11.SOGetTargets(self, count, buffers)
        dp = self.d3d.dp
        if offsets:
            for i in range(count):
                entry = self.so_targets[i] if i < len(self.so_targets) else None
                dp.write(offsets + 4 * i, struct.pack("<I", entry[1] if entry else 0))

    def _shader_object(self, bytecode, length, out, interface):
        state = State11(self.d3d, interface, None)
        try:
            state.data = dxbc.Shader(bytes(self.d3d.dp.read(bytecode, length)))
        except (dxbc.Unsupported, struct.error, ValueError) as e:
            state.error = str(e)
            missing(self.d3d.dp, f"Direct3D 10 shader: {e}")
        return self.dxgi.out(state, interface, out)

    def CreateBlendState(self, desc, out):
        data = blend10_to_11(bytes(self.d3d.dp.read(desc, 68)))
        return self.dxgi.out(State11(self.d3d, "ID3D10BlendState", data), "ID3D10BlendState", out)

    def CreateBlendState1(self, desc, out):
        data = bytes(self.d3d.dp.read(desc, 8 + 32 * 8))
        return self.dxgi.out(State11(self.d3d, "ID3D10BlendState1", data), "ID3D10BlendState1", out)

    def _state10(self, desc, size, out, interface):
        return self.dxgi.out(State11(self.d3d, interface, bytes(self.d3d.dp.read(desc, size))), interface, out)

    def CreateDepthStencilState(self, desc, out):
        return self._state10(desc, 52, out, "ID3D10DepthStencilState")

    def CreateRasterizerState(self, desc, out):
        return self._state10(desc, 40, out, "ID3D10RasterizerState")

    def CreateSamplerState(self, desc, out):
        return self._state10(desc, 52, out, "ID3D10SamplerState")

    def CreateQuery(self, desc, out):
        return self._state10(desc, 8, out, "ID3D10Query")

    def CreatePredicate(self, desc, out):
        return self._state10(desc, 8, out, "ID3D10Predicate")

    def SetTextFilterSize(self, width, height):
        return None

    def GetTextFilterSize(self, width, height):
        dp = self.d3d.dp
        if width:
            dp.write(width, struct.pack("<I", 0))
        if height:
            dp.write(height, struct.pack("<I", 0))

    # ID3D10Multithread
    def Enter(self):
        return None

    def Leave(self):
        return None

    def SetMultithreadProtected(self, value):
        return 1

    def GetMultithreadProtected(self):
        return 1


class Direct3D10(Direct3D11):
    """The Direct3D 10 of a process: its devices, on the classes of Direct3D 11."""

    def device_out(self, out):
        if not self.devices:
            return E_FAIL
        self.dp.write_ptr(out, self.com.create(self.devices[-1], "ID3D10Device"))
        return 0

    def create10(self, dp, adapter, driver, software, flags, level, sdk, device_out, chain_desc=0, chain_out=0):
        device = Device10(self, level or 0xA000, flags)
        self.devices.append(device)
        if device_out:
            dp.write_ptr(device_out, self.com.create(device, "ID3D10Device1" if level else "ID3D10Device"))
        if chain_desc and chain_out:
            result = self.dxgi.factory.CreateSwapChain(self.com.create(device, "ID3D10Device"), chain_desc, chain_out)
            if result:
                return result
        self.dxgi.events.append(dict(kind="direct3d10_device", level=hex(level or 0xA000)))
        return 0 if device_out or chain_out else S_FALSE


def install(dp):
    if numpy is None:
        return None
    d3d = Direct3D10(dp)
    dp.direct3d10 = d3d
    hooks = dp.apihooks
    hooks.register_signature("d3d10.dll", "D3D10CreateDevice", "pipiip",
                             lambda dp, a, t, s, f, sdk, out: d3d.create10(dp, a, t, s, f, 0, sdk, out))
    hooks.register_signature("d3d10.dll", "D3D10CreateDeviceAndSwapChain", "pipiippp",
                             lambda dp, a, t, s, f, sdk, desc, chain, out: d3d.create10(dp, a, t, s, f, 0, sdk, out, desc, chain))
    hooks.register_signature("d3d10_1.dll", "D3D10CreateDevice1", "pipiiip",
                             lambda dp, a, t, s, f, level, sdk, out: d3d.create10(dp, a, t, s, f, level, sdk, out))
    hooks.register_signature("d3d10_1.dll", "D3D10CreateDeviceAndSwapChain1", "pipiiippp",
                             lambda dp, a, t, s, f, level, sdk, desc, chain, out: d3d.create10(dp, a, t, s, f, level, sdk, out, desc, chain))
    for module in ("d3d10.dll", "d3d10_1.dll"):
        hooks.register_signature(module, "D3D10CreateEffectFromMemory", "ppippp",
                                 lambda dp, *args: create_effect(d3d, dp, *args))
    return d3d
