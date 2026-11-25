# Fathom addition to dumpulator: the effects of Direct3D 10 (D3D10CreateEffectFromMemory, the fx_4_0 /
# fx_4_1 binary of fxc): ID3D10Effect, its techniques, passes and variables. Boost Software License 1.0,
# like the rest of dumpulator (LICENSE.dumpulator).
#
# The FX10 part of the effect's DXBC container is parsed (its layout as Wine's d3d10/effect.c reads it:
# local constant buffers and their numeric variables with default values, object variables (shaders,
# textures, samplers, blend / depth stencil / rasterizer states with their property assignments),
# techniques and passes). The variables write into the constant buffers' memory (SetFloat, SetFloatVector,
# SetMatrix, SetRawValue...), and ID3D10EffectPass::Apply sets the pass's shaders on the device with
# their constant buffers, textures and samplers at the bind points of each shader's reflection (RDEF), and
# the pass's states. Not covered: effect pools (shared variables), expressions (preshaders) in property
# assignments, render target / depth stencil view assignments.

import struct
from typing import Dict, List, Optional

from . import dxbc
from .apihooks import read_astring
from .dxgi_emu import missing

E_FAIL = 0x80004005
FLOAT_MAX = 3.402823466e38

# the types of the variables (D3D10_SHADER_VARIABLE_TYPE) of the object type codes of FX10
OBJECT_TYPES = {1: 4, 2: 24, 3: 23, 4: 22, 5: 15, 6: 16, 7: 21, 8: 21, 9: 5, 10: 6, 11: 28, 12: 7, 13: 29, 14: 32, 15: 33,
                16: 8, 17: 9, 19: 30, 20: 31, 21: 10, 22: 25}
NUMERIC_TYPES = {1: 3, 2: 2, 3: 19, 4: 1}       # float, int, uint, bool
SHADERS = {15: "ps", 16: "vs", 21: "gs"}
TEXTURES = {5, 6, 7, 8, 9, 25, 28, 29, 32, 33, 34}
# property ids (the order of Wine's property_infos)
PASS_PROPERTIES = {0: "rasterizer", 1: "depth_stencil", 2: "blend", 6: "vs", 7: "ps", 8: "gs", 9: "stencil_ref",
                   10: "blend_factor", 11: "sample_mask"}
STATE_PROPERTIES = {
    12: ("FillMode", 1), 13: ("CullMode", 1), 14: ("FrontCounterClockwise", 1), 15: ("DepthBias", 1), 16: ("DepthBiasClamp", 1),
    17: ("SlopeScaledDepthBias", 1), 18: ("DepthClipEnable", 1), 19: ("ScissorEnable", 1), 20: ("MultisampleEnable", 1),
    21: ("AntialiasedLineEnable", 1),
    22: ("DepthEnable", 1), 23: ("DepthWriteMask", 1), 24: ("DepthFunc", 1), 25: ("StencilEnable", 1), 26: ("StencilReadMask", 1),
    27: ("StencilWriteMask", 1), 28: ("FrontFail", 1), 29: ("FrontDepthFail", 1), 30: ("FrontPass", 1), 31: ("FrontFunc", 1),
    32: ("BackFail", 1), 33: ("BackDepthFail", 1), 34: ("BackPass", 1), 35: ("BackFunc", 1),
    36: ("AlphaToCoverageEnable", 1), 37: ("BlendEnable", 1), 38: ("SrcBlend", 1), 39: ("DestBlend", 1), 40: ("BlendOp", 1),
    41: ("SrcBlendAlpha", 1), 42: ("DestBlendAlpha", 1), 43: ("BlendOpAlpha", 1), 44: ("RenderTargetWriteMask", 1),
    45: ("Filter", 1), 46: ("AddressU", 1), 47: ("AddressV", 1), 48: ("AddressW", 1), 49: ("MipLODBias", 1),
    50: ("MaxAnisotropy", 1), 51: ("ComparisonFunc", 1), 52: ("BorderColor", 4), 53: ("MinLOD", 1), 54: ("MaxLOD", 1),
    55: ("Texture", 1)}
DEFAULTS = {
    22: dict(FillMode=3, CullMode=3, FrontCounterClockwise=0, DepthBias=0, DepthBiasClamp=0.0, SlopeScaledDepthBias=0.0,
             DepthClipEnable=1, ScissorEnable=0, MultisampleEnable=0, AntialiasedLineEnable=0),
    23: dict(DepthEnable=1, DepthWriteMask=1, DepthFunc=2, StencilEnable=0, StencilReadMask=0xFF, StencilWriteMask=0xFF,
             FrontFail=1, FrontDepthFail=1, FrontPass=1, FrontFunc=8, BackFail=1, BackDepthFail=1, BackPass=1, BackFunc=8),
    24: dict(AlphaToCoverageEnable=0, BlendEnable=[0] * 8, SrcBlend=5, DestBlend=6, BlendOp=1, SrcBlendAlpha=5,
             DestBlendAlpha=6, BlendOpAlpha=1, RenderTargetWriteMask=[0xF] * 8),
    10: dict(Filter=0, AddressU=1, AddressV=1, AddressW=1, MipLODBias=0.0, MaxAnisotropy=16, ComparisonFunc=1,
             BorderColor=[0.0] * 4, MinLOD=0.0, MaxLOD=FLOAT_MAX, Texture=None),
}
FLOAT_PROPERTIES = {"DepthBiasClamp", "SlopeScaledDepthBias", "MipLODBias", "BorderColor", "MinLOD", "MaxLOD"}


def _u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def _string(data: bytes, offset: int) -> str:
    end = data.find(b"\0", offset)
    return data[offset:end if end >= 0 else len(data)].decode("latin-1")


class EffectType:
    interface = "ID3D10EffectType"

    def __init__(self, effect, name, kind, cls, base, rows=0, cols=0, elements=0, unpacked=0, stride=0, packed=0, members=None,
                 flags=0):
        self.effect, self.name, self.kind, self.cls, self.base = effect, name, kind, cls, base
        self.rows, self.cols, self.elements = rows, cols, elements
        self.unpacked, self.stride, self.packed = unpacked, stride, packed
        self.members = members or []      # (name, semantic, offset, EffectType)
        self.flags = flags
        self.name_address = 0

    def element(self):
        return EffectType(self.effect, self.name, self.kind, self.cls, self.base, self.rows, self.cols, 0, self.stride or self.unpacked,
                          self.stride, self.packed // max(self.elements, 1), self.members, self.flags)

    def IsValid(self):
        return 1

    def GetDesc(self, out):
        dp = self.effect.dp
        if not self.name_address:
            self.name_address = self.effect.text(self.name)
        dp.write_ptr(out, self.name_address)
        ptr = dp.ptr_size()
        dp.write(out + ptr, struct.pack("<8I", self.cls, self.base, self.elements, len(self.members), self.rows, self.cols,
                                       self.packed, self.unpacked) + struct.pack("<I", self.stride))
        return 0

    def GetMemberTypeByIndex(self, index):
        return self.effect.pointer(self.members[index][3], "ID3D10EffectType") if index < len(self.members) else 0

    def GetMemberTypeByName(self, name):
        text = read_astring(self.effect.dp, name) or ""
        return next((self.effect.pointer(m[3], "ID3D10EffectType") for m in self.members if m[0] == text), 0)

    def GetMemberTypeBySemantic(self, semantic):
        text = (read_astring(self.effect.dp, semantic) or "").upper()
        return next((self.effect.pointer(m[3], "ID3D10EffectType") for m in self.members if (m[1] or "").upper() == text), 0)

    def GetMemberName(self, index):
        return self.effect.text(self.members[index][0]) if index < len(self.members) else 0

    def GetMemberSemantic(self, index):
        return self.effect.text(self.members[index][1] or "") if index < len(self.members) else 0


class ShaderEntry:
    """A shader of the effect: its bytecode, the shader and its resource bindings (name -> (type, point, count))."""

    def __init__(self, effect, kind, bytecode: bytes, so_declaration=None):
        self.effect, self.kind, self.bytecode, self.so_declaration = effect, kind, bytecode, so_declaration
        self.shader = None
        self.bindings: Dict[str, tuple] = {}
        self.address = 0
        self.state = None
        if not bytecode:
            return
        try:
            self.shader = dxbc.Shader(bytecode)
        except (dxbc.Unsupported, struct.error, ValueError) as e:
            missing(effect.dp, f"Direct3D 10 effect shader: {e}")
        parts = dxbc.chunks(bytecode)
        rdef = parts.get(b"RDEF")
        if rdef:
            count, offset = _u32(rdef, 8), _u32(rdef, 12)
            major = rdef[17]
            minor = rdef[16]
            size = 40 if (major, minor) >= (5, 1) else 32
            for k in range(count):
                base = offset + size * k
                name = _string(rdef, _u32(rdef, base))
                kind, point, number = _u32(rdef, base + 4), _u32(rdef, base + 20), _u32(rdef, base + 24)
                self.bindings[name] = (kind, point, number)

    def code_address(self):
        if not self.address and self.bytecode:
            dp = self.effect.dp
            self.address = dp.allocate(len(self.bytecode))
            dp.write(self.address, self.bytecode)
        return self.address


class EffectVariable:
    """A variable of the effect (numeric in a constant buffer, an object, a constant buffer itself); all the
    variable interfaces are this one class."""

    def __init__(self, effect, name, semantic, etype: EffectType, buffer=None, offset=0):
        self.effect, self.name, self.semantic, self.type = effect, name, semantic, etype
        self.buffer, self.offset = buffer, offset           # numeric: the constant buffer, the offset in it
        self.flags = 0
        self.bind_point = 0xFFFFFFFF
        self.annotations: List["EffectVariable"] = []
        self.members: List["EffectVariable"] = []           # struct members / constant buffer variables
        self.elements: Optional[List["EffectVariable"]] = None
        self.objects: List[object] = []                     # per element: ShaderEntry, state dict, view impl, string
        self.data = None                                    # a constant buffer: its bytes
        self.resource = None                                # a constant buffer: its buffer object
        self.dirty = True
        self.string_address = 0
        self.valid = True

    # navigation ---------------------------------------------------------------
    def IsValid(self):
        return int(self.valid)

    def _me(self, interface):
        return self.effect.pointer(self, interface)

    def GetType(self):
        return self.effect.pointer(self.type, "ID3D10EffectType")

    def GetDesc(self, out):
        dp = self.effect.dp
        ptr = dp.ptr_size()
        dp.write_ptr(out, self.effect.text(self.name))
        dp.write_ptr(out + ptr, self.effect.text(self.semantic) if self.semantic else 0)
        dp.write(out + 2 * ptr, struct.pack("<4I", self.flags, len(self.annotations), self.offset, self.bind_point))
        return 0

    def GetAnnotationByIndex(self, index):
        return self.effect.variable_pointer(self.annotations[index] if index < len(self.annotations) else None)

    def GetAnnotationByName(self, name):
        text = read_astring(self.effect.dp, name) or ""
        return self.effect.variable_pointer(next((a for a in self.annotations if a.name == text), None))

    def GetMemberByIndex(self, index):
        return self.effect.variable_pointer(self.members[index] if index < len(self.members) else None)

    def GetMemberByName(self, name):
        text = read_astring(self.effect.dp, name) or ""
        return self.effect.variable_pointer(next((m for m in self.members if m.name == text), None))

    def GetMemberBySemantic(self, semantic):
        text = (read_astring(self.effect.dp, semantic) or "").upper()
        return self.effect.variable_pointer(next((m for m in self.members if (m.semantic or "").upper() == text), None))

    def GetElement(self, index):
        elements = self._elements()
        return self.effect.variable_pointer(elements[index] if elements and index < len(elements) else None)

    def _elements(self):
        if self.elements is None and self.type.elements:
            item_type = self.type.element()
            self.elements = []
            for k in range(self.type.elements):
                element = EffectVariable(self.effect, self.name, self.semantic, item_type, self.buffer,
                                         self.offset + k * self.type.stride)
                element.objects = self.objects[k:k + 1] if self.objects else []
                element.parent_objects, element.parent_index = self.objects, k
                element.members = [EffectVariable(self.effect, m.name, m.semantic, m.type, self.buffer,
                                                  m.offset + k * self.type.stride) for m in self.members]
                self.elements.append(element)
        return self.elements

    def GetParentConstantBuffer(self):
        return self.effect.variable_pointer(self.buffer, "ID3D10EffectConstantBuffer")

    def _as(self, interface, wanted):
        return self._me(interface) if wanted(self) else self.effect.variable_pointer(None, interface)

    def AsScalar(self):
        return self._as("ID3D10EffectScalarVariable", lambda v: v.type.kind == "numeric")

    def AsVector(self):
        return self._as("ID3D10EffectVectorVariable", lambda v: v.type.kind == "numeric")

    def AsMatrix(self):
        return self._as("ID3D10EffectMatrixVariable", lambda v: v.type.kind == "numeric")

    def AsString(self):
        return self._as("ID3D10EffectStringVariable", lambda v: v.type.base == 4)

    def AsShaderResource(self):
        return self._as("ID3D10EffectShaderResourceVariable", lambda v: v.type.base in TEXTURES)

    def AsRenderTargetView(self):
        return self._as("ID3D10EffectRenderTargetViewVariable", lambda v: v.type.base == 30)

    def AsDepthStencilView(self):
        return self._as("ID3D10EffectDepthStencilViewVariable", lambda v: v.type.base == 31)

    def AsConstantBuffer(self):
        return self._as("ID3D10EffectConstantBuffer", lambda v: v.type.base in (26, 27))

    def AsShader(self):
        return self._as("ID3D10EffectShaderVariable", lambda v: v.type.base in SHADERS)

    def AsBlend(self):
        return self._as("ID3D10EffectBlendVariable", lambda v: v.type.base == 24)

    def AsDepthStencil(self):
        return self._as("ID3D10EffectDepthStencilVariable", lambda v: v.type.base == 23)

    def AsRasterizer(self):
        return self._as("ID3D10EffectRasterizerVariable", lambda v: v.type.base == 22)

    def AsSampler(self):
        return self._as("ID3D10EffectSamplerVariable", lambda v: v.type.base == 10)

    # numeric values -------------------------------------------------------------
    def _write(self, offset, data: bytes):
        if self.buffer is None or self.buffer.data is None:
            return E_FAIL
        start = self.offset + offset
        data = data[:max(0, len(self.buffer.data) - start)]
        self.buffer.data[start:start + len(data)] = data
        self.buffer.dirty = True
        return 0

    def _read(self, offset, size) -> bytes:
        if self.buffer is None or self.buffer.data is None:
            return bytes(size)
        start = self.offset + offset
        return bytes(self.buffer.data[start:start + size]).ljust(size, b"\0")

    def SetRawValue(self, data, offset, count):
        return self._write(offset, bytes(self.effect.dp.read(data, count)))

    def GetRawValue(self, data, offset, count):
        self.effect.dp.write(data, self._read(offset, count))
        return 0

    def _convert(self, values, source):
        """Values (of a source kind: f, i, b) to the variable's base type, as 32-bit words."""
        base = self.type.base
        out = []
        for value in values:
            if base == 3:       # float
                out.append(struct.pack("<f", float(value)))
            elif base == 1:     # bool
                out.append(struct.pack("<I", 1 if value else 0))
            elif base == 19:    # uint
                out.append(struct.pack("<I", int(value) & 0xFFFFFFFF))
            else:               # int
                out.append(struct.pack("<i", max(-(1 << 31), min((1 << 31) - 1, int(value)))))
        return out

    def _set_values(self, address, count, source, per_element, offset=0, elements=1):
        """count values per element from the program (source: f float, i int, b BOOL), elements of stride 16
        (registers of the vector), from element offset."""
        dp = self.effect.dp
        fmt = {"f": "f", "i": "i", "b": "i"}[source]
        stride = self.type.stride or 16
        for e in range(elements):
            values = struct.unpack(f"<{count}{fmt}", bytes(dp.read(address + 4 * count * e, 4 * count)))
            words = self._convert(values, source)
            self._write((offset + e) * stride if elements > 1 or offset else 0, b"".join(words))
        return 0

    def SetFloat(self, value):
        return self._write(0, b"".join(self._convert([value], "f")))

    def SetInt(self, value):
        value = value - (1 << 32) if value & 0x80000000 else value
        return self._write(0, b"".join(self._convert([value], "i")))

    def SetBool(self, value):
        return self._write(0, b"".join(self._convert([value], "b")))

    def _get_scalar(self, out, fmt):
        word = self._read(0, 4)
        base = self.type.base
        value = struct.unpack("<f" if base == 3 else "<i", word)[0]
        self.effect.dp.write(out, struct.pack("<" + fmt, float(value) if fmt == "f" else int(value)))
        return 0

    def GetFloat(self, out):
        return self._get_scalar(out, "f")

    def GetInt(self, out):
        return self._get_scalar(out, "i")

    def GetBool(self, out):
        return self._get_scalar(out, "i")

    def _scalar_array(self, data, offset, count, source):
        dp = self.effect.dp
        fmt = {"f": "f", "i": "i", "b": "i"}[source]
        values = struct.unpack(f"<{count}{fmt}", bytes(dp.read(data, 4 * count)))
        stride = self.type.stride or 16
        for k, value in enumerate(values):
            self._write((offset + k) * stride, self._convert([value], source)[0])
        return 0

    def SetFloatArray(self, data, offset, count):
        return self._scalar_array(data, offset, count, "f")

    def SetIntArray(self, data, offset, count):
        return self._scalar_array(data, offset, count, "i")

    def SetBoolArray(self, data, offset, count):
        return self._scalar_array(data, offset, count, "b")

    def _vector(self, data, source, offset=0, count=1):
        size = max(self.type.cols, 1)
        dp = self.effect.dp
        fmt = {"f": "f", "i": "i", "b": "i"}[source]
        stride = self.type.stride or 16
        for e in range(count):
            values = struct.unpack(f"<4{fmt}", bytes(dp.read(data + 16 * e, 16)))[:size]
            self._write((offset + e) * stride if count > 1 or offset else 0, b"".join(self._convert(values, source)))
        return 0

    def SetFloatVector(self, data):
        return self._vector(data, "f")

    def SetIntVector(self, data):
        return self._vector(data, "i")

    def SetBoolVector(self, data):
        return self._vector(data, "b")

    def SetFloatVectorArray(self, data, offset, count):
        return self._vector(data, "f", offset, count)

    def SetIntVectorArray(self, data, offset, count):
        return self._vector(data, "i", offset, count)

    def SetBoolVectorArray(self, data, offset, count):
        return self._vector(data, "b", offset, count)

    def GetFloatVector(self, out):
        size = max(self.type.cols, 1)
        values = struct.unpack(f"<{size}f", self._read(0, 4 * size))
        self.effect.dp.write(out, struct.pack(f"<{size}f", *values))
        return 0

    GetIntVector = GetFloatVector
    GetBoolVector = GetFloatVector

    def _matrix(self, data, transpose, offset=0, count=1):
        """A matrix (16 floats, row after row) into the registers of the variable (a register per row, or
        per column for a column_major matrix)."""
        dp = self.effect.dp
        rows, cols = max(self.type.rows, 1), max(self.type.cols, 1)
        stride = self.type.stride or 64
        for e in range(count):
            m = struct.unpack("<16f", bytes(dp.read(data + 64 * e, 64)))
            value = [[m[r * 4 + c] for c in range(4)] for r in range(4)]
            if transpose:
                value = [[value[c][r] for c in range(4)] for r in range(4)]
            column_major = self.type.cls == 3
            registers = cols if column_major else rows
            out = bytearray()
            for k in range(registers):
                if column_major:
                    items = [value[r][k] for r in range(rows)]
                else:
                    items = [value[k][c] for c in range(cols)]
                out += struct.pack(f"<{len(items)}f", *items).ljust(16, b"\0")
            self._write((offset + e) * stride if count > 1 or offset else 0, bytes(out[:max(0, (registers - 1) * 16 + 4 *
                                                                                          (rows if column_major else cols))]))
        return 0

    def SetMatrix(self, data):
        return self._matrix(data, False)

    def SetMatrixTranspose(self, data):
        return self._matrix(data, True)

    def SetMatrixArray(self, data, offset, count):
        return self._matrix(data, False, offset, count)

    def SetMatrixTransposeArray(self, data, offset, count):
        return self._matrix(data, True, offset, count)

    def GetMatrix(self, out):
        rows, cols = max(self.type.rows, 1), max(self.type.cols, 1)
        column_major = self.type.cls == 3
        value = [[0.0] * 4 for _ in range(4)]
        for k in range(cols if column_major else rows):
            items = struct.unpack("<4f", self._read(16 * k, 16))
            for j in range(rows if column_major else cols):
                if column_major:
                    value[j][k] = items[j]
                else:
                    value[k][j] = items[j]
        self.effect.dp.write(out, struct.pack("<16f", *[v for row in value for v in row]))
        return 0

    # objects -------------------------------------------------------------------
    def _set_object(self, index, value):
        objects = getattr(self, "parent_objects", None)
        index = getattr(self, "parent_index", index)
        target = objects if objects is not None else self.objects
        while len(target) <= index:
            target.append(None)
        target[index] = value
        if objects is not None:
            self.objects = [value]

    def SetResource(self, view):
        self._set_object(0, self.effect.com.impl(view) if view else None)
        return 0

    def GetResource(self, out):
        view = self.objects[0] if self.objects else None
        self.effect.dp.write_ptr(out, getattr(view, "com_address", 0) or 0)
        return 0

    def SetResourceArray(self, views, offset, count):
        dp = self.effect.dp
        for k in range(count):
            address = dp.read_ptr(views + dp.ptr_size() * k)
            self._set_object(offset + k, self.effect.com.impl(address) if address else None)
        return 0

    def GetString(self, out):
        text = self.objects[0] if self.objects and isinstance(self.objects[0], str) else ""
        self.effect.dp.write_ptr(out, self.effect.text(text))
        return 0

    def GetShaderDesc(self, index, out):
        entry = self.objects[index] if index < len(self.objects) else None
        if not isinstance(entry, ShaderEntry):
            return E_FAIL
        dp = self.effect.dp
        ptr = dp.ptr_size()
        code = entry.code_address()
        isg = len(entry.shader.inputs) if entry.shader else 0
        osg = len(entry.shader.outputs) if entry.shader else 0
        # D3D10_EFFECT_SHADER_DESC: input signature, IsInline, bytecode, length, SO declaration, counts
        dp.write_ptr(out, code)
        dp.write(out + ptr, struct.pack("<I", 0))
        dp.write_ptr(out + 2 * ptr, code)
        dp.write(out + 3 * ptr, struct.pack("<I", len(entry.bytecode)))
        dp.write_ptr(out + 4 * ptr, self.effect.text(entry.so_declaration) if entry.so_declaration else 0)
        dp.write(out + 5 * ptr, struct.pack("<2I", isg, osg))
        return 0

    def _shader_out(self, index, out, interface):
        entry = self.objects[index] if index < len(self.objects) else None
        state = self.effect.shader_state(entry) if isinstance(entry, ShaderEntry) else None
        self.effect.dp.write_ptr(out, self.effect.com.create(state, interface) if state is not None else 0)
        return 0 if state is not None else E_FAIL

    def GetVertexShader(self, index, out):
        return self._shader_out(index, out, "ID3D10VertexShader")

    def GetPixelShader(self, index, out):
        return self._shader_out(index, out, "ID3D10PixelShader")

    def GetGeometryShader(self, index, out):
        return self._shader_out(index, out, "ID3D10GeometryShader")

    def _state_out(self, index, out):
        state = self.effect.state_object(self.type.base, self.objects[index] if index < len(self.objects) else None)
        interface = {24: "ID3D10BlendState", 23: "ID3D10DepthStencilState", 22: "ID3D10RasterizerState",
                     10: "ID3D10SamplerState"}[self.type.base]
        self.effect.dp.write_ptr(out, self.effect.com.create(state, interface) if state is not None else 0)
        return 0

    GetBlendState = GetDepthStencilState = GetRasterizerState = GetSampler = _state_out

    def SetConstantBuffer(self, buffer):
        self.resource = self.effect.com.impl(buffer) if buffer else None
        self.user_buffer = self.resource is not None
        return 0

    def GetConstantBuffer(self, out):
        self.effect.dp.write_ptr(out, getattr(self.effect.constant_resource(self), "com_address", 0) or 0)
        return 0


class EffectPass:
    interface = "ID3D10EffectPass"

    def __init__(self, effect, name):
        self.effect, self.name = effect, name
        self.annotations: List[EffectVariable] = []
        self.shaders: Dict[str, tuple] = {}         # stage -> (variable, element)
        self.states: Dict[str, tuple] = {}          # rasterizer / depth_stencil / blend -> (variable, element)
        self.stencil_ref, self.blend_factor, self.sample_mask = 0, (1.0, 1.0, 1.0, 1.0), 0xFFFFFFFF
        self.valid = True

    def IsValid(self):
        return int(self.valid)

    def GetDesc(self, out):
        dp = self.effect.dp
        ptr = dp.ptr_size()
        vs = self._entry("vs")
        dp.write_ptr(out, self.effect.text(self.name))
        dp.write(out + ptr, struct.pack("<I", len(self.annotations)))
        dp.write_ptr(out + 2 * ptr, vs.code_address() if vs is not None else 0)
        dp.write_ptr(out + 3 * ptr, len(vs.bytecode) if vs is not None else 0)
        dp.write(out + 4 * ptr, struct.pack("<2I4f", self.stencil_ref, self.sample_mask, *self.blend_factor))
        return 0

    def _entry(self, stage) -> Optional[ShaderEntry]:
        found = self.shaders.get(stage)
        if found is None:
            return None
        variable, element = found
        entry = variable.objects[element] if element < len(variable.objects) else None
        return entry if isinstance(entry, ShaderEntry) and entry.bytecode else None

    def _shader_desc(self, stage, out):
        dp = self.effect.dp
        found = self.shaders.get(stage)
        dp.write_ptr(out, self.effect.variable_pointer(found[0] if found else None, "ID3D10EffectShaderVariable"))
        dp.write(out + dp.ptr_size(), struct.pack("<I", found[1] if found else 0))
        return 0

    def GetVertexShaderDesc(self, out):
        return self._shader_desc("vs", out)

    def GetGeometryShaderDesc(self, out):
        return self._shader_desc("gs", out)

    def GetPixelShaderDesc(self, out):
        return self._shader_desc("ps", out)

    def GetAnnotationByIndex(self, index):
        return self.effect.variable_pointer(self.annotations[index] if index < len(self.annotations) else None)

    def GetAnnotationByName(self, name):
        text = read_astring(self.effect.dp, name) or ""
        return self.effect.variable_pointer(next((a for a in self.annotations if a.name == text), None))

    def Apply(self, flags):
        self.effect.apply(self)
        return 0

    def ComputeStateBlockMask(self, mask):
        return 0


class EffectTechnique:
    interface = "ID3D10EffectTechnique"

    def __init__(self, effect, name):
        self.effect, self.name = effect, name
        self.passes: List[EffectPass] = []
        self.annotations: List[EffectVariable] = []
        self.valid = True

    def IsValid(self):
        return int(self.valid)

    def GetDesc(self, out):
        dp = self.effect.dp
        dp.write_ptr(out, self.effect.text(self.name))
        dp.write(out + dp.ptr_size(), struct.pack("<2I", len(self.passes), len(self.annotations)))
        return 0

    def GetAnnotationByIndex(self, index):
        return self.effect.variable_pointer(self.annotations[index] if index < len(self.annotations) else None)

    def GetAnnotationByName(self, name):
        text = read_astring(self.effect.dp, name) or ""
        return self.effect.variable_pointer(next((a for a in self.annotations if a.name == text), None))

    def GetPassByIndex(self, index):
        found = self.passes[index] if index < len(self.passes) else None
        return self.effect.pointer(found or self.effect.null_pass, "ID3D10EffectPass")

    def GetPassByName(self, name):
        text = read_astring(self.effect.dp, name) or ""
        found = next((p for p in self.passes if p.name == text), None)
        return self.effect.pointer(found or self.effect.null_pass, "ID3D10EffectPass")

    def ComputeStateBlockMask(self, mask):
        return 0


class Effect:
    """ID3D10Effect: the parsed FX10 of an effect for a device."""
    interface = "ID3D10Effect"

    def __init__(self, d3d, device, blob: bytes):
        self.d3d, self.device, self.dp, self.com = d3d, device, d3d.dp, d3d.com
        self.buffers: List[EffectVariable] = []
        self.objects: List[EffectVariable] = []
        self.techniques: List[EffectTechnique] = []
        self.types: Dict[int, EffectType] = {}
        self.texts: Dict[str, int] = {}
        self.states: Dict[int, object] = {}
        self.null_variable = EffectVariable(self, "", "", EffectType(self, "", "object", 4, 0))
        self.null_variable.valid = False
        self.null_pass = EffectPass(self, "")
        self.null_pass.valid = False
        self.null_technique = EffectTechnique(self, "")
        self.null_technique.valid = False
        parts = dxbc.chunks(blob)
        data = parts.get(b"FX10") or parts.get(b"FXLC")
        if data is None:
            raise ValueError("no FX10 part")
        self._parse(data)

    # helpers --------------------------------------------------------------------
    def text(self, value: str) -> int:
        if value not in self.texts:
            raw = value.encode("latin-1") + b"\0"
            address = self.dp.allocate(len(raw))
            self.dp.write(address, raw)
            self.texts[value] = address
        return self.texts[value]

    def pointer(self, impl, interface):
        return self.com.create(impl, interface)

    def variable_pointer(self, variable, interface="ID3D10EffectVariable"):
        return self.com.create(variable if variable is not None else self.null_variable, interface)

    # parsing --------------------------------------------------------------------
    def _parse(self, fx: bytes):
        header = struct.unpack_from("<19I", fx, 0)
        (version, local_buffers, numeric_count, object_count, shared_buffers, _, shared_objects, techniques,
         unstructured, strings, textures, ds_count, blend_count, rs_count, sampler_count, rtv_count, dsv_count,
         shader_count, anonymous_count) = header
        self.version = version
        data = fx[19 * 4:]
        self.data = data
        pos = [unstructured]

        def u32():
            value = _u32(data, pos[0])
            pos[0] += 4
            return value

        for _ in range(local_buffers):
            self.buffers.append(self._buffer(u32))
        for _ in range(object_count):
            self.objects.append(self._object(u32))
        if shared_buffers or shared_objects:
            missing(self.dp, "Direct3D 10 effect pools (shared variables)")
        for _ in range(techniques):
            self.techniques.append(self._technique(u32))

    def _type(self, offset: int) -> EffectType:
        if offset in self.types:
            return self.types[offset]
        data = self.data
        name = _string(data, _u32(data, offset))
        kind, elements, unpacked, stride, packed = struct.unpack_from("<5I", data, offset + 4)
        at = offset + 24
        if kind == 1:   # numeric
            info = _u32(data, at)
            cls, base, rows, cols, column_major = info & 7, (info >> 3) & 31, (info >> 8) & 7, (info >> 11) & 7, (info >> 14) & 1
            klass = {1: 0, 2: 1, 3: 3 if column_major else 2}.get(cls, 0)
            etype = EffectType(self, name, "numeric", klass, NUMERIC_TYPES.get(base, 0), rows, cols, elements, unpacked, stride, packed)
        elif kind == 2:     # object
            code = _u32(data, at)
            etype = EffectType(self, name, "object", 4, OBJECT_TYPES.get(code, 0), 0, 0, elements, unpacked, stride, packed,
                               flags=1 if code == 8 else 0)
        else:               # struct
            count = _u32(data, at)
            members = []
            for k in range(count):
                base = at + 4 + 16 * k
                member_name = _string(data, _u32(data, base))
                semantic_offset = _u32(data, base + 4)
                semantic = _string(data, semantic_offset) if semantic_offset else ""
                members.append((member_name, semantic, _u32(data, base + 8), self._type(_u32(data, base + 12))))
            etype = EffectType(self, name, "struct", 5, 0, 0, 0, elements, unpacked, stride, packed, members)
        self.types[offset] = etype
        return etype

    def _annotations(self, u32, count):
        out = []
        for _ in range(count):
            name = _string(self.data, u32())
            etype = self._type(u32())
            value_offset = u32()
            annotation = EffectVariable(self, name, "", etype)
            if etype.base == 4:
                annotation.objects = [_string(self.data, value_offset)]
            out.append(annotation)
        return out

    def _buffer(self, u32) -> EffectVariable:
        name = _string(self.data, u32())
        size, flags, count, bind_point = u32(), u32(), u32(), u32()
        annotations = self._annotations(u32, u32())
        etype = EffectType(self, "tbuffer" if flags & 1 else "cbuffer", "object", 4, 27 if flags & 1 else 26)
        buffer = EffectVariable(self, name, "", etype)
        buffer.data = bytearray(size)
        buffer.bind_point = bind_point
        buffer.annotations = annotations
        for _ in range(count):
            buffer.members.append(self._numeric(u32, buffer))
        return buffer

    def _numeric(self, u32, buffer) -> EffectVariable:
        name = _string(self.data, u32())
        etype = self._type(u32())
        semantic_offset = u32()
        semantic = _string(self.data, semantic_offset) if semantic_offset else ""
        offset, default, flags = u32(), u32(), u32()
        variable = EffectVariable(self, name, semantic, etype, buffer, offset)
        variable.flags = flags
        if default:
            self._default(variable, etype, offset, default)
        variable.annotations = self._annotations(u32, u32())
        variable.members = self._members(variable)
        return variable

    def _members(self, variable):
        return [EffectVariable(self, name, semantic, mtype, variable.buffer, variable.offset + offset)
                for name, semantic, offset, mtype in variable.type.members]

    def _default(self, variable, etype, offset, at):
        """The packed default value of a numeric variable, laid out in the buffer."""
        data = self.data
        packed = data[at:at + etype.packed]
        buffer = variable.buffer.data
        position = [0]

        def place(t, where):
            if t.kind == "struct":
                for count in range(max(t.elements, 1)):
                    for _, _, member_offset, member in t.members:
                        place(member, where + count * t.stride + member_offset)
                return
            for count in range(max(t.elements, 1)):
                base = where + count * t.stride
                rows, cols = max(t.rows, 1), max(t.cols, 1)
                column_major = t.cls == 3
                values = [packed[position[0] + 4 * k:position[0] + 4 * k + 4] for k in range(rows * cols)]
                position[0] += 4 * rows * cols
                for r in range(rows):
                    for c in range(cols):
                        word = values[r * cols + c]
                        place_at = base + (16 * c + 4 * r if column_major else 16 * r + 4 * c)
                        buffer[place_at:place_at + 4] = word
        place(etype, offset)

    def _object(self, u32) -> EffectVariable:
        name = _string(self.data, u32())
        etype = self._type(u32())
        semantic_offset = u32()
        semantic = _string(self.data, semantic_offset) if semantic_offset else ""
        bind_point = u32()
        variable = EffectVariable(self, name, semantic, etype)
        variable.bind_point = bind_point
        count = max(etype.elements, 1)
        base = etype.base
        if base in TEXTURES or base in (30, 31):
            variable.objects = [None] * count
        elif base in SHADERS:
            for _ in range(count):
                shader_offset = u32()
                so = None
                if etype.flags & 1:
                    so_offset = u32()
                    so = _string(self.data, so_offset) if so_offset else None
                variable.objects.append(self._shader(SHADERS[base], shader_offset, so))
        elif base in (22, 23, 24, 10):
            for _ in range(count):
                desc = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS[base].items()}
                for _ in range(u32()):
                    self._assignment(u32, desc, None)
                variable.objects.append(desc)
        elif base == 4:
            pass
        variable.annotations = self._annotations(u32, u32())
        return variable

    def _shader(self, kind, offset, so=None) -> ShaderEntry:
        size = _u32(self.data, offset)
        return ShaderEntry(self, kind, bytes(self.data[offset + 4:offset + 4 + size]), so)

    def _technique(self, u32) -> EffectTechnique:
        technique = EffectTechnique(self, _string(self.data, u32()))
        passes = u32()
        technique.annotations = self._annotations(u32, u32())
        for _ in range(passes):
            p = EffectPass(self, _string(self.data, u32()))
            assignments = u32()
            p.annotations = self._annotations(u32, u32())
            for _ in range(assignments):
                self._assignment(u32, None, p)
            technique.passes.append(p)
        return technique

    def variable_named(self, name) -> Optional[EffectVariable]:
        for buffer in self.buffers:
            for v in buffer.members:
                if v.name == name:
                    return v
        return next((v for v in self.objects if v.name == name), None)

    def _values(self, offset):
        """A value list: [(kind, word)] -> python values."""
        count = _u32(self.data, offset)
        out = []
        for k in range(count):
            kind, word = struct.unpack_from("<2I", self.data, offset + 4 + 8 * k)
            if kind == 1:
                out.append(struct.unpack("<f", struct.pack("<I", word))[0])
            elif kind == 2:
                out.append(struct.unpack("<i", struct.pack("<I", word))[0])
            else:
                out.append(word)
        return out

    def _assignment(self, u32, desc, p):
        """A property assignment of a state (desc) or of a pass (p)."""
        pid, index, operation, value_offset = u32(), u32(), u32(), u32()
        data = self.data
        if operation == 7:       # an anonymous shader: (shader offset, stream output declaration)
            shader_offset, so_offset = struct.unpack_from("<2I", data, value_offset)
            stage = {6: "vs", 7: "ps", 8: "gs"}.get(pid)
            if p is not None and stage:
                entry = self._shader(stage, shader_offset, _string(data, so_offset) if so_offset else None)
                holder = EffectVariable(self, f"<anonymous {stage}>", "", EffectType(self, stage + "shader", "object", 4,
                                                                                        {"vs": 16, "ps": 15, "gs": 21}[stage]))
                holder.objects = [entry]
                p.shaders[stage] = (holder, 0)
            return
        variable, element = None, 0
        if operation in (2, 3):
            if operation == 2:
                name = _string(data, value_offset)
            else:
                name_offset, element = struct.unpack_from("<2I", data, value_offset)
                name = _string(data, name_offset)
            variable = self.variable_named(name)
        elif operation in (4, 5, 6):
            missing(self.dp, "Direct3D 10 effect expressions (preshaders)")
            return
        if p is not None:
            key = PASS_PROPERTIES.get(pid)
            if key in ("vs", "ps", "gs"):
                if variable is not None:
                    p.shaders[key] = (variable, element)
            elif key in ("rasterizer", "depth_stencil", "blend"):
                if variable is not None:
                    p.states[key] = (variable, element)
            elif key in ("stencil_ref", "blend_factor", "sample_mask") and operation == 1:
                values = self._values(value_offset)
                if key == "blend_factor":
                    p.blend_factor = tuple(float(v) for v in values[:4])
                else:
                    setattr(p, key, int(values[0]) & 0xFFFFFFFF)
            return
        if desc is None or pid not in STATE_PROPERTIES:
            return
        field, size = STATE_PROPERTIES[pid]
        if field == "Texture":
            desc["Texture"] = variable
            return
        if operation != 1:
            return      # a numeric property from a variable: kept at its default
        values = self._values(value_offset)
        if field in ("BlendEnable", "RenderTargetWriteMask"):
            desc[field][index] = int(values[0])
        elif size > 1:
            desc[field] = [float(v) for v in values[:size]]
        else:
            desc[field] = float(values[0]) if field in FLOAT_PROPERTIES else int(values[0])

    # what the device gets ----------------------------------------------------------
    def shader_state(self, entry: ShaderEntry):
        if entry is None or entry.shader is None:
            return None
        if entry.state is None:
            from .d3d11_emu import State11
            interface = {"vs": "ID3D10VertexShader", "ps": "ID3D10PixelShader", "gs": "ID3D10GeometryShader"}[entry.kind]
            entry.state = State11(self.d3d, interface, entry.shader)
        return entry.state

    def state_object(self, base, desc):
        """The state object (D3D11 descriptions, which the device reads) of a state of the effect."""
        if desc is None:
            return None
        key = id(desc)
        if key in self.states:
            return self.states[key][1]
        from .d3d11_emu import State11
        if base == 22:
            data = struct.pack("<3Ii2f4I", desc["FillMode"], desc["CullMode"], desc["FrontCounterClockwise"], desc["DepthBias"],
                               desc["DepthBiasClamp"], desc["SlopeScaledDepthBias"], desc["DepthClipEnable"], desc["ScissorEnable"],
                               desc["MultisampleEnable"], desc["AntialiasedLineEnable"])
            state = State11(self.d3d, "ID3D10RasterizerState", data)
        elif base == 23:
            data = struct.pack("<3I", desc["DepthEnable"], desc["DepthWriteMask"], desc["DepthFunc"]) + \
                struct.pack("<I2B2x", desc["StencilEnable"], desc["StencilReadMask"], desc["StencilWriteMask"]) + \
                struct.pack("<8I", desc["FrontFail"], desc["FrontDepthFail"], desc["FrontPass"], desc["FrontFunc"],
                            desc["BackFail"], desc["BackDepthFail"], desc["BackPass"], desc["BackFunc"])
            state = State11(self.d3d, "ID3D10DepthStencilState", data)
        elif base == 24:
            data = struct.pack("<2I", desc["AlphaToCoverageEnable"], 1)
            for k in range(8):
                data += struct.pack("<7IB3x", desc["BlendEnable"][k], desc["SrcBlend"], desc["DestBlend"], desc["BlendOp"],
                                    desc["SrcBlendAlpha"], desc["DestBlendAlpha"], desc["BlendOpAlpha"],
                                    desc["RenderTargetWriteMask"][k] & 0xF)
            state = State11(self.d3d, "ID3D10BlendState", data)
        else:
            data = struct.pack("<4If2I4f2f", desc["Filter"], desc["AddressU"], desc["AddressV"], desc["AddressW"], desc["MipLODBias"],
                               desc["MaxAnisotropy"], desc["ComparisonFunc"], *desc["BorderColor"], desc["MinLOD"], desc["MaxLOD"])
            state = State11(self.d3d, "ID3D10SamplerState", data)
        self.states[key] = (desc, state)
        return state

    def constant_resource(self, buffer: EffectVariable):
        """The constant buffer of the device holding a buffer's variables (written when they changed)."""
        if getattr(buffer, "user_buffer", False):
            return buffer.resource
        if buffer.resource is None:
            from .d3d10_emu import Resource10
            size = (len(buffer.data) + 15) // 16 * 16 or 16
            desc = struct.pack("<5I", size, 0, 4, 0, 0)     # D3D10_BUFFER_DESC: default usage, constant buffer
            buffer.resource = Resource10(self.d3d, "buffer", desc, "ID3D10Buffer")
            self.com.create(buffer.resource, "ID3D10Buffer")
            buffer.dirty = True
        if buffer.dirty:
            buffer.resource.write(bytes(buffer.data))
            buffer.dirty = False
        return buffer.resource

    def apply(self, p: EffectPass):
        device = self.device
        for stage in ("vs", "gs", "ps"):
            entry = p._entry(stage)
            state = self.shader_state(entry) if entry is not None else None
            device.shaders[stage] = state
            if entry is None:
                continue
            for name, (kind, point, count) in entry.bindings.items():
                if kind in (0, 1):      # cbuffer / tbuffer
                    buffer = next((b for b in self.buffers if b.name == name), None)
                    if buffer is not None:
                        device.constant_buffers[(stage, point)] = (self.constant_resource(buffer), 0, 0)
                elif kind == 2:         # texture
                    variable = self.variable_named(name.split("[")[0])
                    if variable is not None:
                        for k in range(max(count, 1)):
                            view = variable.objects[k] if k < len(variable.objects) else None
                            device.resources[(stage, point + k)] = view
                elif kind == 3:         # sampler
                    variable = self.variable_named(name.split("[")[0])
                    if variable is not None:
                        for k in range(max(count, 1)):
                            desc = variable.objects[k] if k < len(variable.objects) else None
                            device.sampler_states[(stage, point + k)] = self.state_object(10, desc)
        for key, base in (("rasterizer", 22), ("depth_stencil", 23), ("blend", 24)):
            found = p.states.get(key)
            if found is None:
                continue
            variable, element = found
            desc = variable.objects[element] if element < len(variable.objects) else None
            state = self.state_object(base, desc)
            if key == "rasterizer":
                device.rasterizer = state
            elif key == "depth_stencil":
                device.depth_state = state
            else:
                device.blend = state
                device.blend_factor = p.blend_factor

    # ID3D10Effect -----------------------------------------------------------------
    def IsValid(self):
        return 1

    def IsPool(self):
        return 0

    def GetDevice(self, out):
        self.dp.write_ptr(out, self.com.create(self.device, "ID3D10Device"))
        return 0

    def GetDesc(self, out):
        variables = sum(len(b.members) for b in self.buffers) + len(self.objects)
        self.dp.write(out, struct.pack("<6I", 0, len(self.buffers), 0, variables, 0, len(self.techniques)))
        return 0

    def GetConstantBufferByIndex(self, index):
        return self.variable_pointer(self.buffers[index] if index < len(self.buffers) else None, "ID3D10EffectConstantBuffer")

    def GetConstantBufferByName(self, name):
        text = read_astring(self.dp, name) or ""
        return self.variable_pointer(next((b for b in self.buffers if b.name == text), None), "ID3D10EffectConstantBuffer")

    def _all_variables(self):
        return [v for b in self.buffers for v in b.members] + self.objects

    def GetVariableByIndex(self, index):
        variables = self._all_variables()
        return self.variable_pointer(variables[index] if index < len(variables) else None)

    def GetVariableByName(self, name):
        return self.variable_pointer(self.variable_named(read_astring(self.dp, name) or ""))

    def GetVariableBySemantic(self, semantic):
        text = (read_astring(self.dp, semantic) or "").upper()
        return self.variable_pointer(next((v for v in self._all_variables() if (v.semantic or "").upper() == text), None))

    def GetTechniqueByIndex(self, index):
        found = self.techniques[index] if index < len(self.techniques) else None
        return self.pointer(found or self.null_technique, "ID3D10EffectTechnique")

    def GetTechniqueByName(self, name):
        text = read_astring(self.dp, name) or ""
        found = next((t for t in self.techniques if t.name == text), None)
        return self.pointer(found or self.null_technique, "ID3D10EffectTechnique")

    def Optimize(self):
        return 0

    def IsOptimized(self):
        return 0


def create_effect(d3d, dp, data, size, flags, device, pool, out):
    """D3D10CreateEffectFromMemory."""
    impl = d3d.com.impl(device) if device else None
    if impl is None or not data or not out:
        return 0x80070057    # E_INVALIDARG
    try:
        effect = Effect(d3d, impl, bytes(dp.read(data, size)))
    except (ValueError, struct.error, dxbc.Unsupported, KeyError, IndexError) as e:
        missing(dp, f"Direct3D 10 effect: {e}")
        dp.write_ptr(out, 0)
        return E_FAIL
    dp.write_ptr(out, d3d.com.create(effect, "ID3D10Effect"))
    return 0
