# Fathom addition to dumpulator: OpenGL 1.2 to 4.x for the emulated program, on top of the OpenGL 1.1 of
# opengl_emu.py. Boost Software License 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# wglGetProcAddress gives an entry point for every function of glext.h / wglext.h (graphics_tables.py):
# those implemented here run, the others do nothing and are named in the summary. Buffers (their bytes in
# the emulator's memory, glMapBuffer(Range) gives that memory), vertex array objects and generic
# attributes (all the types, normalized, integer, instanced), shaders and programs in GLSL (glsl.py:
# vertex and fragment shaders of both profiles; link, attribute / uniform / fragment output locations,
# uniform blocks in std140), texture objects of every target (2D, cube, 3D, arrays, rectangle; mipmaps,
# S3TC compression) and sampler objects, framebuffer and renderbuffer objects (several draw buffers,
# glBlitFramebuffer), instanced and base-vertex draws, separate blending, queries, syncs;
# wglCreateContextAttribsARB, wglChoosePixelFormatARB, wglSwapIntervalEXT, the extension strings.
# Geometry, tessellation (the Direct3D 11 tessellator of dxbc.py) and compute shaders (glDispatchCompute),
# shader storage blocks, images and atomic counters run too (glsl.py).

import struct
from typing import Dict, List, Optional

from . import glsl
from .apihooks import read_args, read_astring, signature_slots
from .graphics_tables import OPENGL_EXT, WGL_EXT
from .opengl_emu import (BLEND_FACTORS, Context, GL_BLEND, GL_CULL_FACE, GL_DEPTH_TEST, GL_FRONT_AND_BACK, GL_BACK,
                         GL_CCW, GL_SCISSOR_TEST, GL_TEXTURE_2D, GL_MODELVIEW, GL_PROJECTION, KINDS, OpenGl, _i, _sign)
from .raster import Blend, Output, Pipeline, Target, draw, kind_of, numpy, primitives, sample

GL_ARRAY_BUFFER, GL_ELEMENT_ARRAY_BUFFER, GL_UNIFORM_BUFFER = 0x8892, 0x8893, 0x8A11
GL_FRAMEBUFFER, GL_READ_FRAMEBUFFER, GL_DRAW_FRAMEBUFFER, GL_RENDERBUFFER = 0x8D40, 0x8CA8, 0x8CA9, 0x8D41
GL_COLOR_ATTACHMENT0, GL_DEPTH_ATTACHMENT, GL_STENCIL_ATTACHMENT, GL_DEPTH_STENCIL_ATTACHMENT = 0x8CE0, 0x8D00, 0x8D20, 0x821A
GL_TEXTURE_CUBE_MAP, GL_TEXTURE_3D, GL_TEXTURE_2D_ARRAY, GL_TEXTURE_RECTANGLE, GL_TEXTURE_1D = 0x8513, 0x806F, 0x8C1A, 0x84F5, 0x0DE0
GL_TEXTURE_CUBE_MAP_POSITIVE_X = 0x8515
VERSION = "3.3.0 Fathom emulator"
EXTENSIONS = ["GL_ARB_vertex_array_object", "GL_ARB_framebuffer_object", "GL_ARB_vertex_buffer_object",
              "GL_ARB_shader_objects", "GL_ARB_vertex_shader", "GL_ARB_fragment_shader", "GL_ARB_shading_language_100",
              "GL_ARB_texture_non_power_of_two", "GL_ARB_uniform_buffer_object", "GL_ARB_instanced_arrays",
              "GL_ARB_draw_instanced", "GL_ARB_map_buffer_range", "GL_ARB_sampler_objects", "GL_ARB_sync",
              "GL_EXT_texture_compression_s3tc", "GL_EXT_framebuffer_object", "GL_EXT_framebuffer_blit",
              "GL_ARB_texture_float", "GL_ARB_depth_texture", "GL_EXT_texture_filter_anisotropic"]
WGL_EXTENSIONS = "WGL_ARB_extensions_string WGL_ARB_create_context WGL_ARB_create_context_profile WGL_ARB_pixel_format " \
                 "WGL_EXT_swap_control WGL_ARB_framebuffer_sRGB"
SUFFIXES = ("ARB", "EXT", "OES", "KHR", "NV", "AMD", "APPLE", "ATI", "SGIS", "SGIX", "IBM", "MESA", "INTEL")


# ------------------------------------------------------------------ objects
class GLTexture:
    def __init__(self, name, target=0):
        self.name, self.target = name, target
        self.levels: Dict[tuple, numpy.ndarray] = {}    # (layer, level) -> (h, w, 4) float32, row 0 = t 0
        self.params: Dict[int, int] = {}
        self.format = 0x1908

    def level(self, level=0, layer=0) -> Optional[numpy.ndarray]:
        return self.levels.get((layer, level)) if (layer, level) in self.levels else self.levels.get((0, level))


class GLBuffer:
    def __init__(self, gl, name):
        self.gl, self.name = gl, name
        self.address, self.size, self.usage = 0, 0, 0
        self.mapped = False

    def data(self) -> numpy.ndarray:
        if not self.size:
            return numpy.zeros(0, dtype=numpy.uint8)
        return numpy.frombuffer(bytes(self.gl.dp.read(self.address, self.size)), dtype=numpy.uint8)

    def allocate(self, size):
        if size > self.size or not self.address:
            self.address = self.gl.dp.allocate(max(16, size), page_align=True)
        self.size = size


class GLVertexArray:
    def __init__(self, name):
        self.name = name
        self.attribs: Dict[int, dict] = {}
        self.element_buffer: Optional[GLBuffer] = None


class GLShader:
    def __init__(self, name, kind):
        self.name, self.kind = name, kind
        self.source = ""
        self.program: Optional[glsl.Program] = None
        self.log = ""
        self.compiled = False


class GLProgram:
    def __init__(self, name):
        self.name = name
        self.shaders: List[GLShader] = []
        self.linked = False
        self.log = ""
        self.vertex: Optional[glsl.Program] = None
        self.fragment: Optional[glsl.Program] = None
        self.attrib_bindings: Dict[str, int] = {}
        self.frag_bindings: Dict[str, int] = {}
        self.attributes: Dict[str, int] = {}             # name -> location
        self.attribute_kinds: Dict[str, str] = {}
        self.uniforms: Dict[str, glsl.Val] = {}          # top-level name -> value (n = 1)
        self.uniform_kinds: Dict[str, tuple] = {}
        self.locations: Dict[int, tuple] = {}            # location -> (path, kind)
        self.names: Dict[str, int] = {}                  # "a", "a[2]", "s.f" -> location
        self.blocks: List[dict] = []
        self.outputs: Dict[str, int] = {}
        self.geometry: Optional[glsl.Program] = None
        self.tess_control: Optional[glsl.Program] = None
        self.tess_eval: Optional[glsl.Program] = None
        self.compute: Optional[glsl.Program] = None
        self.storage_blocks: List[str] = []              # the buffer blocks (glGetProgramResourceIndex)
        self.storage_bindings: Dict[str, int] = {}       # block -> binding (glShaderStorageBlockBinding)

    def stages(self):
        return [s for s in (self.vertex, self.tess_control, self.tess_eval, self.geometry, self.fragment, self.compute)
                if s is not None]


class GLFramebuffer:
    def __init__(self, name):
        self.name = name
        self.attachments: Dict[int, tuple] = {}          # attachment -> (kind, object, level, layer)
        self.draw_buffers: List[int] = [GL_COLOR_ATTACHMENT0]
        self.read_buffer = GL_COLOR_ATTACHMENT0


class GLRenderbuffer:
    def __init__(self, name):
        self.name = name
        self.format, self.width, self.height = 0, 0, 0
        self.color: Optional[numpy.ndarray] = None
        self.depth: Optional[numpy.ndarray] = None


def _extend_context():
    original = Context.__init__

    def init(self, gl, hdc):
        original(self, gl, hdc)
        self.version = (1, 1)
        self.texture_objects: Dict[int, GLTexture] = {}
        self.units: Dict[tuple, int] = {}         # (unit, target) -> texture name
        self.active_unit = 0
        self.sampler_objects: Dict[int, dict] = {}
        self.unit_samplers: Dict[int, int] = {}
        self.buffers: Dict[int, GLBuffer] = {}
        self.bindings: Dict[int, Optional[GLBuffer]] = {}
        self.indexed_bindings: Dict[tuple, tuple] = {}
        self.vertex_arrays: Dict[int, GLVertexArray] = {0: GLVertexArray(0)}
        self.vertex_array = self.vertex_arrays[0]
        self.generic = {k: [0.0, 0.0, 0.0, 1.0] for k in range(16)}
        self.shaders: Dict[int, GLShader] = {}
        self.programs: Dict[int, GLProgram] = {}
        self.program: Optional[GLProgram] = None
        self.framebuffers: Dict[int, GLFramebuffer] = {}
        self.renderbuffers: Dict[int, GLRenderbuffer] = {}
        self.draw_fb: Optional[GLFramebuffer] = None
        self.read_fb: Optional[GLFramebuffer] = None
        self.renderbuffer: Optional[GLRenderbuffer] = None
        self.next_name = 1
        self.queries: Dict[int, list] = {}
        self.query_running: Dict[int, int] = {}
        self.polygon_mode = 0x1B02  # GL_FILL
        self.restart_index = None
        self.swap_interval = 1
        self.normal = [0.0, 0.0, 1.0]
        self.provoking = "last"
        self.client_unit = 0
        self.patch_vertices = 3
        self.patch_outer = [1.0, 1.0, 1.0, 1.0]
        self.patch_inner = [1.0, 1.0]
        self.image_units: Dict[int, tuple] = {}   # unit -> (texture, level, layered, layer, access, format)
    Context.__init__ = init


_extend_context()


# ------------------------------------------------------------------ the methods (added to OpenGl)
class GLMore:
    """Methods mixed into opengl_emu.OpenGl (the gl functions are the methods _glName)."""

    def _new_names(self, c, count, out):
        count = _i(count)
        names = list(range(c.next_name, c.next_name + count))
        c.next_name += count
        if out:
            self.dp.write(out, struct.pack(f"<{count}I", *names))
        return names

    def _names(self, count, address):
        count = _i(count)
        return struct.unpack(f"<{count}I", bytes(self.dp.read(address, 4 * count))) if count and address else ()

    # textures ---------------------------------------------------------------
    def _glGenTextures(self, c, count, out):
        for name in self._new_names(c, count, out):
            c.texture_objects[name] = GLTexture(name)

    _glCreateTextures = None

    def _glDeleteTextures(self, c, count, names):
        for name in self._names(count, names):
            c.texture_objects.pop(name, None)
            for key in [k for k, v in c.units.items() if v == name]:
                del c.units[key]

    def _glIsTexture(self, c, name):
        return int(name in c.texture_objects)

    def _glActiveTexture(self, c, unit):
        c.active_unit = max(0, unit - 0x84C0)

    def _glClientActiveTexture(self, c, unit):
        c.client_unit = max(0, unit - 0x84C0)

    def _target_of(self, target):
        if GL_TEXTURE_CUBE_MAP_POSITIVE_X <= target <= GL_TEXTURE_CUBE_MAP_POSITIVE_X + 5:
            return GL_TEXTURE_CUBE_MAP
        return target

    def _glBindTexture(self, c, target, name):
        texture = c.texture_objects.get(name)
        if texture is None and name:
            texture = c.texture_objects[name] = GLTexture(name, target)
        if texture is not None and not texture.target:
            texture.target = target
        c.units[(c.active_unit, target)] = name
        if target == GL_TEXTURE_2D and c.active_unit == 0:
            c.bound = name

    def _bound_texture(self, c, target, unit=None) -> Optional[GLTexture]:
        name = c.units.get((c.active_unit if unit is None else unit, self._target_of(target)), 0)
        texture = c.texture_objects.get(name)
        if texture is None:
            texture = c.texture_objects.setdefault(0 - (1 + (unit or 0)) * 100 - self._target_of(target), GLTexture(0, target))
        return texture

    def _glTexParameteri(self, c, target, name, value):
        self._bound_texture(c, target).params[name] = value

    def _glTexParameterf(self, c, target, name, value):
        self._bound_texture(c, target).params[name] = int(value)

    def _glTexParameteriv(self, c, target, name, values):
        self._bound_texture(c, target).params[name] = struct.unpack("<i", bytes(self.dp.read(values, 4)))[0]

    def _glTexParameterfv(self, c, target, name, values):
        self._bound_texture(c, target).params[name] = int(struct.unpack("<f", bytes(self.dp.read(values, 4)))[0])

    def _layer_of(self, target):
        return target - GL_TEXTURE_CUBE_MAP_POSITIVE_X if GL_TEXTURE_CUBE_MAP_POSITIVE_X <= target <= \
            GL_TEXTURE_CUBE_MAP_POSITIVE_X + 5 else 0

    def _glTexImage2D(self, c, target, level, internal, width, height, border, fmt, kind, data):
        texture = self._bound_texture(c, target)
        width, height = _i(width), _i(height)
        texture.format = internal
        pixels = self._unpack(c, width, height, fmt, kind, data) if data or c.bindings.get(0x88EC) else None
        if pixels is None:
            pixels = numpy.zeros((max(1, height), max(1, width), 4), numpy.float32)
            pixels[..., 3] = 1
        if target == GL_TEXTURE_1D:
            pixels = pixels[:1]
        texture.levels[(self._layer_of(target), _i(level))] = pixels

    def _glTexImage1D(self, c, target, level, internal, width, border, fmt, kind, data):
        self._glTexImage2D(c, target, level, internal, width, 1, border, fmt, kind, data)

    def _glTexImage3D(self, c, target, level, internal, width, height, depth, border, fmt, kind, data):
        texture = self._bound_texture(c, target)
        width, height, depth = _i(width), _i(height), _i(depth)
        size = self._pixel_size(fmt, kind) * width * height
        for layer in range(depth):
            pixels = self._unpack(c, width, height, fmt, kind, data + layer * size) if data else None
            if pixels is None:
                pixels = numpy.zeros((max(1, height), max(1, width), 4), numpy.float32)
            texture.levels[(layer, _i(level))] = pixels

    def _glTexStorage2D(self, c, target, levels, internal, width, height):
        texture = self._bound_texture(c, target)
        width, height = _i(width), _i(height)
        faces = 6 if target == GL_TEXTURE_CUBE_MAP else 1
        for face in range(faces):
            for level in range(_i(levels)):
                texture.levels[(face, level)] = numpy.zeros((max(1, height >> level), max(1, width >> level), 4), numpy.float32)
        texture.format = internal

    def _glTexStorage3D(self, c, target, levels, internal, width, height, depth):
        texture = self._bound_texture(c, target)
        for layer in range(_i(depth)):
            for level in range(_i(levels)):
                texture.levels[(layer, level)] = numpy.zeros((max(1, _i(height) >> level), max(1, _i(width) >> level), 4),
                                                             numpy.float32)

    def _glTexStorage1D(self, c, target, levels, internal, width):
        self._glTexStorage2D(c, target, levels, internal, width, 1)

    def _glTexSubImage2D(self, c, target, level, x, y, width, height, fmt, kind, data):
        texture = self._bound_texture(c, target)
        array = texture.levels.get((self._layer_of(target), _i(level)))
        if array is None:
            return
        x, y, width, height = _i(x), _i(y), _i(width), _i(height)
        pixels = self._unpack(c, width, height, fmt, kind, data)
        if pixels is not None:
            array[y:y + height, x:x + width] = pixels[:array.shape[0] - y, :array.shape[1] - x]

    def _glTexSubImage3D(self, c, target, level, x, y, z, width, height, depth, fmt, kind, data):
        texture = self._bound_texture(c, target)
        size = self._pixel_size(fmt, kind) * _i(width) * _i(height)
        for k in range(_i(depth)):
            array = texture.levels.get((_i(z) + k, _i(level)))
            if array is None:
                continue
            pixels = self._unpack(c, _i(width), _i(height), fmt, kind, data + k * size)
            if pixels is not None:
                array[_i(y):_i(y) + _i(height), _i(x):_i(x) + _i(width)] = pixels

    def _glCompressedTexImage2D(self, c, target, level, internal, width, height, border, size, data):
        from .gpu import decode_blocks
        kind = {0x83F0: "bc1", 0x83F1: "bc1", 0x83F2: "bc2", 0x83F3: "bc3", 0x8C4C: "bc1", 0x8C4D: "bc1", 0x8C4E: "bc2",
                0x8C4F: "bc3", 0x8DBB: "bc4", 0x8DBC: "bc4s", 0x8DBD: "bc5", 0x8DBE: "bc5s", 0x8E8C: "bc7",
                0x8E8D: "bc7", 0x8E8E: "bc6hs", 0x8E8F: "bc6h"}.get(internal)
        texture = self._bound_texture(c, target)
        if kind is None:
            self._missing_feature(f"OpenGL compressed format {hex(internal)}")
            return
        if c.bindings.get(0x88EC) is not None:
            data = c.bindings[0x88EC].address + data
        raw = bytes(self.dp.read(data, _i(size)))
        texture.levels[(self._layer_of(target), _i(level))] = decode_blocks(raw, kind, _i(width), _i(height))

    def _glCopyTexImage2D(self, c, target, level, internal, x, y, width, height, border):
        texture = self._bound_texture(c, target)
        texture.levels[(self._layer_of(target), _i(level))] = self._read_region(c, _i(x), _i(y), _i(width), _i(height))

    def _glCopyTexSubImage2D(self, c, target, level, xoffset, yoffset, x, y, width, height):
        array = self._bound_texture(c, target).levels.get((self._layer_of(target), _i(level)))
        if array is not None:
            region = self._read_region(c, _i(x), _i(y), _i(width), _i(height))
            array[_i(yoffset):_i(yoffset) + region.shape[0], _i(xoffset):_i(xoffset) + region.shape[1]] = region

    def _glGenerateMipmap(self, c, target):
        texture = self._bound_texture(c, target)
        for (layer, level), array in list(texture.levels.items()):
            if level != 0:
                continue
            current = array
            k = 1
            while current.shape[0] > 1 or current.shape[1] > 1:
                h, w = max(1, current.shape[0] // 2), max(1, current.shape[1] // 2)
                trimmed = current[:h * 2, :w * 2]
                if trimmed.shape[0] >= 2 and trimmed.shape[1] >= 2:
                    current = trimmed.reshape(h, 2, w, 2, 4).mean(axis=(1, 3)).astype(numpy.float32)
                else:
                    current = numpy.resize(trimmed, (h, w, 4)).astype(numpy.float32)
                texture.levels[(layer, k)] = current
                k += 1

    def _glGetTexImage(self, c, target, level, fmt, kind, out):
        array = self._bound_texture(c, target).levels.get((self._layer_of(target), _i(level)))
        if array is not None:
            self.dp.write(out, self._pack(c, array, fmt, kind))

    def _glGetTexLevelParameteriv(self, c, target, level, name, out):
        array = self._bound_texture(c, target).levels.get((self._layer_of(target), _i(level)))
        value = 0
        if array is not None:
            value = {0x1000: array.shape[1], 0x1001: array.shape[0], 0x8071: 1}.get(name, 0)
        self.dp.write(out, struct.pack("<i", value))

    def _glPixelStoref(self, c, name, value):
        return self._glPixelStorei(c, name, int(value))

    @staticmethod
    def _pixel_size(fmt, kind):
        channels = {0x1908: 4, 0x1907: 3, 0x80E1: 4, 0x80E0: 3, 0x1903: 1, 0x1909: 1, 0x190A: 2, 0x1906: 1, 0x8227: 2,
                    0x1902: 1, 0x8D94: 1, 0x8228: 2, 0x8D98: 3, 0x8D99: 4, 0x84F9: 1}.get(fmt, 4)
        width = {0x1401: 1, 0x1400: 1, 0x1403: 2, 0x1402: 2, 0x1405: 4, 0x1404: 4, 0x1406: 4, 0x140B: 2, 0x8367: 4,
                 0x8035: 4, 0x8368: 4, 0x84FA: 4}.get(kind, 1)
        if kind in (0x8367, 0x8035, 0x8368, 0x84FA):
            return 4
        return channels * width

    def _unpack(self, c, width, height, fmt, kind, address):
        """Pixels of the program (or of the bound pixel unpack buffer) as (h, w, 4) floats, row 0 first."""
        if width <= 0 or height <= 0:
            return None
        if c.bindings.get(0x88EC) is not None:
            address = c.bindings[0x88EC].address + address
        if not address:
            return None
        channels = {0x1908: 4, 0x1907: 3, 0x80E1: 4, 0x80E0: 3, 0x1903: 1, 0x1909: 1, 0x190A: 2, 0x1906: 1, 0x8227: 2,
                    0x1902: 1, 0x8D94: 1, 0x8228: 2, 0x8D98: 3, 0x8D99: 4, 0x84F9: 1}.get(fmt, 4)
        dtype, scale = {0x1401: ("u1", 255.0), 0x1400: ("i1", 127.0), 0x1403: ("<u2", 65535.0), 0x1402: ("<i2", 32767.0),
                        0x1405: ("<u4", 4294967295.0), 0x1404: ("<i4", 2147483647.0), 0x1406: ("<f4", 1.0),
                        0x140B: ("<f2", 1.0)}.get(kind, ("u1", 255.0))
        if kind in (0x8367, 0x8035):    # GL_UNSIGNED_INT_8_8_8_8(_REV)
            dtype, scale, channels = "u1", 255.0, 4
        if fmt in (0x8D94, 0x8228, 0x8D98, 0x8D99, 0x8D9A):  # integer formats: the values
            scale = 1.0
        size = numpy.dtype(dtype).itemsize * channels
        row = (width * size + c.unpack_alignment - 1) // c.unpack_alignment * c.unpack_alignment
        data = numpy.frombuffer(bytes(self.dp.read(address, row * height)), dtype=numpy.uint8).reshape(height, row)
        data = numpy.ascontiguousarray(data[:, :width * size])
        values = data.view(dtype).reshape(height, width, channels).astype(numpy.float32) / scale
        if kind == 0x8367 and fmt == 0x80E1:  # BGRA with 8_8_8_8_REV: B G R A bytes
            pass
        out = numpy.zeros((height, width, 4), dtype=numpy.float32)
        out[..., 3] = 1
        if fmt in (0x80E1, 0x80E0):
            order = [2, 1, 0, 3][:channels]
            out[..., :channels] = values[..., order]
        elif fmt == 0x1906:
            out[..., 3] = values[..., 0]
        elif fmt == 0x1909:
            out[..., :3] = values[..., :1]
        elif fmt == 0x190A:
            out[..., :3], out[..., 3] = values[..., :1], values[..., 1]
        elif fmt == 0x1902:  # GL_DEPTH_COMPONENT
            out[..., 0] = values[..., 0]
        else:
            out[..., :channels] = values
        return out

    def _pack(self, c, array, fmt, kind) -> bytes:
        channels = {0x1908: (0, 1, 2, 3), 0x1907: (0, 1, 2), 0x80E1: (2, 1, 0, 3), 0x80E0: (2, 1, 0), 0x1903: (0,),
                    0x1909: (0,), 0x1906: (3,), 0x8227: (0, 1), 0x1902: (0,)}.get(fmt, (0, 1, 2, 3))
        values = array[..., list(channels)]
        if kind == 0x1406:
            data = values.astype("<f4")
        else:
            data = numpy.round(numpy.clip(values, 0, 1) * 255).astype(numpy.uint8)
        h = array.shape[0]
        row = data.shape[1] * data.shape[2] * data.dtype.itemsize
        padded = (row + c.pack_alignment - 1) // c.pack_alignment * c.pack_alignment
        lines = data.reshape(h, -1).view(numpy.uint8)
        if padded != row:
            lines = numpy.concatenate([lines, numpy.zeros((h, padded - row), numpy.uint8)], 1)
        return lines.tobytes()

    def _texture_function(self, c, unit):
        """The sampling of a texture unit for GLSL: (sampler kind, coords, offset / "fetch" / "size", lod)."""
        def function(kind, coords, mode, lod, bias=None, gradients=None):
            target = {"sampler1D": GL_TEXTURE_1D, "sampler3D": GL_TEXTURE_3D, "samplerCube": GL_TEXTURE_CUBE_MAP,
                      "sampler2DArray": GL_TEXTURE_2D_ARRAY, "sampler2DRect": GL_TEXTURE_RECTANGLE,
                      "samplerCubeShadow": GL_TEXTURE_CUBE_MAP, "sampler2DArrayShadow": GL_TEXTURE_2D_ARRAY,
                      "sampler1DShadow": GL_TEXTURE_1D, "sampler2DRectShadow": GL_TEXTURE_RECTANGLE,
                      "isampler2D": GL_TEXTURE_2D, "usampler2D": GL_TEXTURE_2D}.get(kind, GL_TEXTURE_2D)
            texture = self._bound_texture(c, target, unit)
            return self.sample_texture(c, texture, unit, kind, coords, mode, lod, bias, gradients)
        return function

    def sample_texture(self, c, texture: GLTexture, unit, kind, coords, mode, lod, bias=None, gradients=None):
        n = 0 if coords is None else coords.shape[0]
        params = dict(texture.params) if texture is not None else {}
        sampler = c.sampler_objects.get(c.unit_samplers.get(unit, 0))
        if sampler:
            params.update(sampler)
        base_level = int(params.get(0x813C, 0))                    # GL_TEXTURE_BASE_LEVEL
        if mode == "size":
            level = base_level + (0 if lod is None else int(numpy.asarray(lod).reshape(-1)[0]) if n else 0)
            base = texture.level(level) if texture is not None else None
            return (base.shape[1], base.shape[0], 1) if base is not None else (0, 0, 1)
        base = texture.level(base_level) if texture is not None else None
        if base is None:
            base = texture.level(0) if texture is not None else None
            base_level = 0
        if base is None:
            out = numpy.zeros((n, 4))
            out[:, 3] = 1
            return out
        linear = params.get(0x2800, 0x2601) == 0x2601
        min_filter = params.get(0x2801, 0x2702)
        mip_linear = min_filter in (0x2702, 0x2703)
        modes = {0x812F: "clamp", 0x2900: "clamp", 0x8370: "mirror", 0x812D: "border", 0x2901: "wrap"}
        address = (modes.get(params.get(0x2802, 0x2901), "wrap"), modes.get(params.get(0x2803, 0x2901), "wrap"))
        if mode == "fetch":
            level = base_level + (0 if lod is None else int(numpy.clip(numpy.round(numpy.median(numpy.nan_to_num(lod))), 0, 15)))
            ints = numpy.round(coords).astype(numpy.int64)
            y = ints[:, 1] if coords.shape[1] > 1 and kind != "sampler1D" else numpy.zeros(n, numpy.int64)
            layer = ints[:, 2] if coords.shape[1] > 2 and texture.target in (GL_TEXTURE_2D_ARRAY, GL_TEXTURE_3D) else None
            array = texture.level(level) if layer is None else texture.level(level, int(layer[0]) if n else 0)
            if array is None:
                array = base
            return array[numpy.clip(y, 0, array.shape[0] - 1), numpy.clip(ints[:, 0], 0, array.shape[1] - 1)]
        # the mip chain from the base level, and the level of each lane
        max_level = int(params.get(0x813D, 1000))
        count = 1
        if min_filter in (0x2700, 0x2701, 0x2702, 0x2703) and texture.target != GL_TEXTURE_RECTANGLE:
            while base_level + count <= max_level and texture.level(base_level + count) is not None:
                count += 1
        lanes = None
        if count > 1:
            from .raster import implicit_lod, lod_of_gradients
            if lod is not None:
                lanes = numpy.asarray(lod, dtype=numpy.float64)
            else:
                cube = texture.target == GL_TEXTURE_CUBE_MAP
                if cube:
                    major = numpy.abs(coords[:, :3]).max(axis=1, keepdims=True)
                    points = coords[:, :3] / numpy.where(major == 0, 1, major) * 0.5
                    sizes = (base.shape[1],) * 3
                else:
                    points = coords
                    sizes = (base.shape[1],) if kind in ("sampler1D", "sampler1DShadow") else \
                        (base.shape[1], base.shape[0]) if texture.target != GL_TEXTURE_3D else \
                        (base.shape[1], base.shape[0], len({key[0] for key in texture.levels if key[1] == base_level}))
                k = len(sizes)
                if gradients is not None:
                    gx, gy = (numpy.asarray(g, dtype=numpy.float64)[:, :k] * numpy.asarray(sizes) for g in gradients)
                    if cube:
                        gx, gy = gx / numpy.where(major == 0, 1, major) * 0.5, gy / numpy.where(major == 0, 1, major) * 0.5
                    lanes = lod_of_gradients(gx, gy)
                else:
                    lanes = implicit_lod(points[:, :k], sizes)
                if lanes is not None:
                    lanes = lanes + float(params.get(0x8501, 0)) + (0 if bias is None else bias)   # GL_TEXTURE_LOD_BIAS
            if lanes is not None:
                lanes = numpy.clip(lanes, float(params.get(0x813A, -1000)), float(params.get(0x813B, 1000)))

        def chain(layer=0):
            return [texture.level(base_level + i, layer) for i in range(count)]

        from .raster import sample_mips
        if texture.target == GL_TEXTURE_CUBE_MAP:
            from .gpu import View, Image, Sampler, sample_view
            images = []
            for face in range(6):
                levels = []
                for array in chain(face):
                    image = Image(array.shape[1], array.shape[0], "rgba32f")
                    image.data[...] = array
                    levels.append(image)
                images.append(levels)
            view = View("texture", images=images, dimension="cube", mips=count)
            return sample_view(view, Sampler(linear, ("clamp",) * 3, mip_linear=mip_linear), coords[:, :3],
                               lanes if lanes is not None else numpy.zeros(n))
        u = coords[:, 0]
        v = coords[:, 1] if coords.shape[1] > 1 and kind not in ("sampler1D", "sampler1DShadow") else numpy.full(n, 0.5)
        if texture.target == GL_TEXTURE_RECTANGLE or kind.startswith("sampler2DRect"):
            u, v = u / base.shape[1], v / base.shape[0]
        if isinstance(mode, tuple) and mode != (0, 0):
            u, v = u + mode[0] / base.shape[1], v + mode[1] / base.shape[0]
        if texture.target in (GL_TEXTURE_2D_ARRAY, GL_TEXTURE_3D) and coords.shape[1] > 2:
            layers = sorted({key[0] for key in texture.levels if key[1] == base_level})
            if texture.target == GL_TEXTURE_3D:
                index = numpy.clip(numpy.floor(coords[:, 2] * len(layers)).astype(int), 0, len(layers) - 1)
            else:
                index = numpy.clip(numpy.round(coords[:, 2]).astype(int), 0, len(layers) - 1)
            out = numpy.zeros((n, 4))
            for k in numpy.unique(index):
                sel = index == k
                levels = [level for level in chain(layers[k]) if level is not None]
                out[sel] = sample_mips(levels, u[sel], v[sel], None if lanes is None else lanes[sel], linear, mip_linear,
                                       address)
            return out
        return sample_mips(chain(), u, v, lanes, linear, mip_linear, address)

    def _glGenSamplers(self, c, count, out):
        for name in self._new_names(c, count, out):
            c.sampler_objects[name] = {}

    def _glDeleteSamplers(self, c, count, names):
        for name in self._names(count, names):
            c.sampler_objects.pop(name, None)

    def _glBindSampler(self, c, unit, sampler):
        c.unit_samplers[unit] = sampler

    def _glSamplerParameteri(self, c, sampler, name, value):
        c.sampler_objects.setdefault(sampler, {})[name] = value

    def _glSamplerParameterf(self, c, sampler, name, value):
        c.sampler_objects.setdefault(sampler, {})[name] = int(value)

    # buffers ----------------------------------------------------------------
    def _glGenBuffers(self, c, count, out):
        for name in self._new_names(c, count, out):
            c.buffers[name] = GLBuffer(self, name)

    _glCreateBuffers = _glGenBuffers

    def _glDeleteBuffers(self, c, count, names):
        for name in self._names(count, names):
            buffer = c.buffers.pop(name, None)
            for target, bound in list(c.bindings.items()):
                if bound is buffer:
                    c.bindings[target] = None

    def _glIsBuffer(self, c, name):
        return int(name in c.buffers)

    def _glBindBuffer(self, c, target, name):
        buffer = c.buffers.setdefault(name, GLBuffer(self, name)) if name else None
        if target == GL_ELEMENT_ARRAY_BUFFER:
            c.vertex_array.element_buffer = buffer
        c.bindings[target] = buffer

    def _buffer_of(self, c, target) -> Optional[GLBuffer]:
        if target == GL_ELEMENT_ARRAY_BUFFER:
            return c.vertex_array.element_buffer
        return c.bindings.get(target)

    def _glBufferData(self, c, target, size, data, usage):
        buffer = self._buffer_of(c, target)
        if buffer is None:
            return
        size = int(size)
        buffer.allocate(size)
        buffer.usage = usage
        if data and size:
            self.dp.write(buffer.address, bytes(self.dp.read(data, size)))
        elif size:
            self.dp.write(buffer.address, b"\0" * size)

    def _glBufferSubData(self, c, target, offset, size, data):
        buffer = self._buffer_of(c, target)
        if buffer is not None and data and size:
            self.dp.write(buffer.address + int(offset), bytes(self.dp.read(data, int(size))))

    def _glGetBufferSubData(self, c, target, offset, size, out):
        buffer = self._buffer_of(c, target)
        if buffer is not None:
            self.dp.write(out, bytes(self.dp.read(buffer.address + int(offset), int(size))))

    def _glMapBuffer(self, c, target, access):
        buffer = self._buffer_of(c, target)
        if buffer is None:
            return 0
        buffer.mapped = True
        return buffer.address

    def _glMapBufferRange(self, c, target, offset, length, access):
        buffer = self._buffer_of(c, target)
        if buffer is None:
            return 0
        buffer.mapped = True
        return buffer.address + int(offset)

    def _glUnmapBuffer(self, c, target):
        buffer = self._buffer_of(c, target)
        if buffer is not None:
            buffer.mapped = False
        return 1

    def _glFlushMappedBufferRange(self, c, target, offset, length):
        return None

    def _glCopyBufferSubData(self, c, read_target, write_target, read_offset, write_offset, size):
        source, target = self._buffer_of(c, read_target), self._buffer_of(c, write_target)
        if source is not None and target is not None:
            self.dp.write(target.address + int(write_offset), bytes(self.dp.read(source.address + int(read_offset), int(size))))

    def _glBindBufferBase(self, c, target, index, name):
        buffer = c.buffers.get(name) if name else None
        c.indexed_bindings[(target, index)] = (buffer, 0, buffer.size if buffer else 0)
        c.bindings[target] = buffer

    def _glBindBufferRange(self, c, target, index, name, offset, size):
        buffer = c.buffers.get(name) if name else None
        c.indexed_bindings[(target, index)] = (buffer, int(offset), int(size))
        c.bindings[target] = buffer

    def _glGetBufferParameteriv(self, c, target, name, out):
        buffer = self._buffer_of(c, target)
        value = {0x8764: buffer.size if buffer else 0, 0x8765: buffer.usage if buffer else 0}.get(name, 0)
        self.dp.write(out, struct.pack("<i", value))

    # vertex arrays and attributes ---------------------------------------------
    def _glGenVertexArrays(self, c, count, out):
        for name in self._new_names(c, count, out):
            c.vertex_arrays[name] = GLVertexArray(name)

    _glCreateVertexArrays = _glGenVertexArrays

    def _glDeleteVertexArrays(self, c, count, names):
        for name in self._names(count, names):
            if name:
                c.vertex_arrays.pop(name, None)

    def _glBindVertexArray(self, c, name):
        c.vertex_array = c.vertex_arrays.setdefault(name, GLVertexArray(name))

    def _glIsVertexArray(self, c, name):
        return int(name in c.vertex_arrays)

    def _attrib(self, c, index):
        return c.vertex_array.attribs.setdefault(index, dict(enabled=False, size=4, type=0x1406, normalized=False,
                                                             stride=0, pointer=0, buffer=None, integer=False, divisor=0))

    def _glEnableVertexAttribArray(self, c, index):
        self._attrib(c, index)["enabled"] = True

    def _glDisableVertexAttribArray(self, c, index):
        self._attrib(c, index)["enabled"] = False

    def _glVertexAttribPointer(self, c, index, size, kind, normalized, stride, pointer):
        self._attrib(c, index).update(size=_i(size), type=kind, normalized=bool(normalized), stride=_i(stride),
                                      pointer=pointer, buffer=c.bindings.get(GL_ARRAY_BUFFER), integer=False)

    def _glVertexAttribIPointer(self, c, index, size, kind, stride, pointer):
        self._attrib(c, index).update(size=_i(size), type=kind, normalized=False, stride=_i(stride), pointer=pointer,
                                      buffer=c.bindings.get(GL_ARRAY_BUFFER), integer=True)

    def _glVertexAttribDivisor(self, c, index, divisor):
        self._attrib(c, index)["divisor"] = divisor

    def _glGetVertexAttribiv(self, c, index, name, out):
        attrib = self._attrib(c, index)
        value = {0x8622: int(attrib["enabled"]), 0x8623: attrib["size"], 0x8624: attrib["stride"], 0x8625: attrib["type"],
                 0x886A: int(attrib["normalized"])}.get(name, 0)
        self.dp.write(out, struct.pack("<i", value))

    def _set_generic(self, c, index, values):
        current = (list(values) + [0.0, 0.0, 0.0, 1.0][len(values):])[:4]
        c.generic[index] = current
        if index == 0 and c.begin is not None:    # attribute 0 in glBegin / glEnd: a vertex
            self._vertex(c, *current)

    def _glNormal3f(self, c, x, y, z):
        c.normal = [x, y, z]

    _glNormal3d = _glNormal3f

    def _glMultiTexCoord2f(self, c, unit, s, t):
        if unit == 0x84C0:
            c.texcoord = [s, t]

    # attribute reading --------------------------------------------------------
    def _read_attribute(self, c, attrib, rows):
        """An attribute array of the vertices rows: (n, 4) floats (or integers for the I forms)."""
        n = len(rows)
        kind = attrib["type"]
        size = attrib["size"]
        bgra = size == 0x80E1
        if bgra:
            size = 4
        dtype, scale = {0x1406: ("<f4", 1.0), 0x1401: ("u1", 255.0), 0x1400: ("i1", 127.0), 0x1403: ("<u2", 65535.0),
                        0x1402: ("<i2", 32767.0), 0x1405: ("<u4", 4294967295.0), 0x1404: ("<i4", 2147483647.0),
                        0x140A: ("<f8", 1.0), 0x140B: ("<f2", 1.0), 0x140C: ("<i4", 65536.0)}.get(kind, ("<f4", 1.0))
        packed = kind in (0x8D9F, 0x8368)
        width = 4 if packed else numpy.dtype(dtype).itemsize * size
        stride = attrib["stride"] or width
        buffer = attrib["buffer"]
        rows = numpy.asarray(rows, dtype=numpy.int64)
        if buffer is not None:
            data = buffer.data()
            starts = attrib["pointer"] + rows * stride
        else:
            if not attrib["pointer"] or n == 0:
                return numpy.tile(numpy.array([0.0, 0.0, 0.0, 1.0]), (n, 1))
            top = int(rows.max()) * stride + width
            data = numpy.frombuffer(bytes(self.dp.read(attrib["pointer"], top)), dtype=numpy.uint8)
            starts = rows * stride
        valid = (starts >= 0) & (starts + width <= len(data))
        raw = numpy.zeros((n, width), dtype=numpy.uint8)
        if valid.any():
            raw[valid] = data[starts[valid][:, None] + numpy.arange(width)]
        out = numpy.zeros((n, 4))
        out[:, 3] = 1
        if packed:
            v = raw.view("<u4").reshape(n).astype(numpy.int64)
            parts = [(v >> s) & 1023 for s in (0, 10, 20)] + [v >> 30]
            if kind == 0x8D9F:
                parts = [numpy.where(p >= 512, p - 1024, p) for p in parts[:3]] + [numpy.where(parts[3] >= 2, parts[3] - 4, parts[3])]
            if attrib["normalized"]:
                parts = [p / (511.0 if kind == 0x8D9F else 1023.0) for p in parts[:3]] + [parts[3] / (1.0 if kind == 0x8D9F else 3.0)]
            out[:] = numpy.stack(parts, 1)
            return out
        values = raw.view(dtype).reshape(n, size).astype(numpy.float64)
        if attrib["normalized"] and not attrib["integer"] and dtype not in ("<f4", "<f8", "<f2"):
            values = numpy.maximum(values / scale, -1) if dtype.startswith(("i", "<i")) else values / scale
        if bgra:
            values = values[:, [2, 1, 0, 3]]
        out[:, :size] = values
        return out

    # shaders and programs ------------------------------------------------------
    def _glCreateShader(self, c, kind):
        name = c.next_name
        c.next_name += 1
        c.shaders[name] = GLShader(name, {0x8B31: "vertex", 0x8B30: "fragment", 0x8DD9: "geometry", 0x8E88: "tess_control",
                                          0x8E87: "tess_evaluation", 0x91B9: "compute"}.get(kind, "vertex"))
        return name

    def _glCreateShaderObjectARB(self, c, kind):
        return self._glCreateShader(c, kind)

    def _glShaderSource(self, c, name, count, strings, lengths):
        shader = c.shaders.get(name)
        if shader is None:
            return
        ptr = self.dp.ptr_size()
        parts = []
        for k in range(_i(count)):
            address = self.dp.read_ptr(strings + ptr * k)
            length = _sign(struct.unpack("<I", bytes(self.dp.read(lengths + 4 * k, 4)))[0], 32) if lengths else -1
            if length is not None and length >= 0:
                parts.append(bytes(self.dp.read(address, length)).decode("latin-1"))
            else:
                parts.append(read_astring(self.dp, address, 1 << 20) or "")
        shader.source = "".join(parts)

    def _glCompileShader(self, c, name):
        shader = c.shaders.get(name)
        if shader is None:
            return
        try:
            shader.program = glsl.Program(shader.source, shader.kind)
            shader.compiled, shader.log = True, ""
        except (glsl.GLSLError, IndexError, ValueError, KeyError) as e:
            shader.program, shader.compiled = None, False
            shader.log = f"ERROR: 0:0: {e}\n"
            self.dp.info(f"GLSL: {e}")

    def _glGetShaderiv(self, c, name, parameter, out):
        shader = c.shaders.get(name)
        value = 0
        if shader is not None:
            value = {0x8B81: int(shader.compiled), 0x8B84: len(shader.log) + 1 if shader.log else 0,
                     0x8B4F: {"vertex": 0x8B31, "fragment": 0x8B30, "geometry": 0x8DD9, "tess_control": 0x8E88,
                              "tess_evaluation": 0x8E87, "compute": 0x91B9}.get(shader.kind, 0x8B30),
                     0x8B88: len(shader.source) + 1, 0x8B80: 0}.get(parameter, 0)
        self.dp.write(out, struct.pack("<i", value))

    def _info_log(self, text, size, length, out):
        data = text.encode("latin-1")[:max(0, _i(size) - 1)]
        if out and _i(size) > 0:
            self.dp.write(out, data + b"\0")
        if length:
            self.dp.write(length, struct.pack("<i", len(data)))

    def _glGetShaderInfoLog(self, c, name, size, length, out):
        shader = c.shaders.get(name)
        self._info_log(shader.log if shader else "", size, length, out)

    def _glDeleteShader(self, c, name):
        c.shaders.pop(name, None)

    def _glIsShader(self, c, name):
        return int(name in c.shaders)

    def _glCreateProgram(self, c):
        name = c.next_name
        c.next_name += 1
        c.programs[name] = GLProgram(name)
        return name

    def _glCreateProgramObjectARB(self, c):
        return self._glCreateProgram(c)

    def _glAttachShader(self, c, program, shader):
        p, s = c.programs.get(program), c.shaders.get(shader)
        if p is not None and s is not None and s not in p.shaders:
            p.shaders.append(s)

    def _glDetachShader(self, c, program, shader):
        p, s = c.programs.get(program), c.shaders.get(shader)
        if p is not None and s in p.shaders:
            p.shaders.remove(s)

    def _glBindAttribLocation(self, c, program, index, name):
        p = c.programs.get(program)
        if p is not None:
            p.attrib_bindings[read_astring(self.dp, name) or ""] = index

    def _glBindFragDataLocation(self, c, program, index, name):
        p = c.programs.get(program)
        if p is not None:
            p.frag_bindings[read_astring(self.dp, name) or ""] = index

    def _glLinkProgram(self, c, program):
        p = c.programs.get(program)
        if p is None:
            return
        found = {kind: next((s.program for s in p.shaders if s.kind == kind and s.program is not None), None)
                 for kind in ("vertex", "fragment", "geometry", "tess_control", "tess_evaluation", "compute")}
        p.vertex, p.fragment, p.geometry = found["vertex"], found["fragment"], found["geometry"]
        p.tess_control, p.tess_eval, p.compute = found["tess_control"], found["tess_evaluation"], found["compute"]
        p.storage_blocks = []
        for stage in p.stages():
            for block in stage.blocks:
                if "buffer" in block["qualifiers"] and block["name"] not in p.storage_blocks:
                    p.storage_blocks.append(block["name"])
        if any(not s.compiled for s in p.shaders) or (p.vertex is None and p.fragment is None and p.compute is None):
            p.linked, p.log = False, "ERROR: a shader did not compile\n"
            return
        p.linked, p.log = True, ""
        self._link_attributes(p)
        self._link_uniforms(c, p)
        self._link_outputs(p)

    # the geometry, tessellation and compute stages ----------------------------------------------------
    def _storage(self, c, p):
        """The buffers of the buffer blocks and atomic counters of a program's stages (by the binding in their
        layout), and a function writing them back into the buffer objects."""
        memory, places = {}, []
        for stage in p.stages():
            for block in stage.blocks:
                if "buffer" not in block["qualifiers"]:
                    continue
                layout_binding = glsl._const_count(block["layout"].get("binding")) or 0
                binding = p.storage_bindings.get(block["name"], layout_binding)
                entry = c.indexed_bindings.get((0x90D2, binding))      # GL_SHADER_STORAGE_BUFFER
                if entry is None or entry[0] is None or layout_binding in memory:
                    continue
                buffer, offset, size = entry
                size = size or max(0, buffer.size - offset)
                data = numpy.frombuffer(bytearray(self.dp.read(buffer.address + offset, size)), dtype=numpy.uint8) if size else \
                    numpy.zeros(0, dtype=numpy.uint8)
                memory[layout_binding] = data
                places.append((buffer.address + offset, data))
            for item in stage.globals:
                v = item.value
                if v["kind"] != "atomic_uint":
                    continue
                binding = glsl._const_count(v["layout"].get("binding")) or 0
                entry = c.indexed_bindings.get((0x92C0, binding))      # GL_ATOMIC_COUNTER_BUFFER
                if entry is None or entry[0] is None or ("atomic", binding) in memory:
                    continue
                buffer, offset, size = entry
                size = size or max(0, buffer.size - offset)
                data = numpy.frombuffer(bytearray(self.dp.read(buffer.address + offset, size)), dtype=numpy.uint8)
                memory[("atomic", binding)] = data
                places.append((buffer.address + offset, data))

        def write_back():
            for address, data in places:
                if len(data):
                    self.dp.write(address, data.tobytes())
        return memory, write_back

    def _images(self, c):
        out = {}
        for unit, (name, level, layered, layer, access, fmt) in c.image_units.items():
            texture = c.texture_objects.get(name)
            if texture is None:
                continue
            if layered:
                layers = sorted({key[0] for key in texture.levels if key[1] == level})
                arrays = [texture.level(level, k) for k in layers]
            else:
                arrays = [texture.level(level, layer)]
            arrays = [a for a in arrays if a is not None]
            integer = fmt in (0x8D82, 0x8D70, 0x8235, 0x8236, 0x823C, 0x8D88, 0x8D76, 0x8D8E, 0x8D7C)
            out[unit] = glsl.ImageUnit(arrays, integer, fmt in (0x8236, 0x8D70, 0x823C, 0x8D76, 0x8D7C))
        return out

    def _stage_uniforms(self, c, p, n):
        return self._uniform_values_for(c, p, n)

    def _inputs_from(self, program, previous, rows, extra=None):
        """The array inputs of a stage (geometry, tessellation) read from the previous stage's outputs: item k
        of each, the value of the vertex rows[:, k]; gl_in from gl_Position / gl_PointSize."""
        inputs = dict(extra or {})
        for name, kind, array, layout, quals in program.interface("in"):
            if name.startswith("gl_") or "patch" in quals or not previous.has(name):
                continue
            inputs[name] = _per_vertex(previous.lookup(name), rows)
        for block in program.blocks:
            if "in" in block["qualifiers"] and block["instance"]:
                fields = {}
                for member in block["members"]:
                    if previous.has(member[0]):
                        fields[member[0]] = previous.lookup(member[0])
                for item in getattr(previous, "values", {}).values() if hasattr(previous, "values") else []:
                    if isinstance(item.data, dict):
                        for key, value in item.data.items():
                            if any(m[0] == key for m in block["members"]):
                                fields[key] = value
                if fields:
                    items = [glsl.Val("__block", {k: glsl._rows(v, rows[:, j]) for k, v in fields.items()})
                             for j in range(rows.shape[1])]
                    inputs[block["instance"]] = glsl.Val("array", items, len(items))
        position = previous.lookup("gl_Position") if previous.has("gl_Position") else None
        point = previous.lookup("gl_PointSize") if previous.has("gl_PointSize") else None
        if position is not None:
            items = []
            for j in range(rows.shape[1]):
                fields = {"gl_Position": glsl._rows(position, rows[:, j])}
                if point is not None:
                    fields["gl_PointSize"] = glsl._rows(point, rows[:, j])
                items.append(glsl.Val("__block", fields))
            inputs["gl_in"] = glsl.Val("array", items, len(items))
        return inputs

    def _geometry(self, c, p, stage, textures, memory, images):
        gs = p.geometry
        prims, simple = _assemble(stage.order, stage.kind)
        expected = {"points": 1, "lines": 2, "lines_adjacency": 4, "triangles": 3, "triangles_adjacency": 6}
        layout_in = glsl.program_layout(gs, "in")
        wanted = next((count for name, count in expected.items() if name in layout_in), prims.shape[1] if prims.ndim > 1 else 1)
        if prims.ndim < 2 or prims.shape[1] < wanted:
            return _Stage(_Values({}), 0, [], "points")
        prims = prims[:, :wanted] if wanted not in (4, 6) or simple.endswith("_adj") else prims
        count = len(prims)
        inputs = self._inputs_from(gs, stage.rt, prims, {"gl_PrimitiveIDIn": glsl.Val("int", numpy.arange(count))})
        runs = glsl.run_geometry(gs, count, inputs, self._stage_uniforms(c, p, count), textures, memory, images)
        layout_out = glsl.program_layout(gs, "out")
        size = 1 if "points" in layout_out else 2 if "line_strip" in layout_out else 3
        pieces, made = [], []
        total = 0
        for invocation, events in enumerate(runs):
            strips = [[] for _ in range(count)]
            for event, stream, lanes, snapshot in events:
                if stream != 0:
                    continue
                if event == "cut":
                    for lane in lanes:
                        strips[lane] = []
                    continue
                pieces.append(snapshot)
                ids = range(total, total + len(lanes))
                total += len(lanes)
                for lane, vertex in zip(lanes, ids):
                    strip = strips[lane]
                    strip.append(vertex)
                    if size == 1:
                        made.append((int(lane), invocation, len(made), (vertex,)))
                    elif size == 2 and len(strip) >= 2:
                        made.append((int(lane), invocation, len(made), (strip[-2], strip[-1])))
                    elif size == 3 and len(strip) >= 3:
                        tri = (strip[-3], strip[-2], strip[-1]) if (len(strip) - 3) % 2 == 0 else (strip[-2], strip[-3], strip[-1])
                        made.append((int(lane), invocation, len(made), tri))
        if not made:
            return _Stage(_Values({}), 0, [], "points")
        made.sort(key=lambda item: item[:3])
        names = set(pieces[0])
        for piece in pieces[1:]:
            names &= set(piece)
        values = {name: _concat([piece[name] for piece in pieces]) for name in names}
        for value in list(values.values()):     # the members of output blocks by their own names
            if isinstance(value.data, dict) and value.kind == "__block":
                for key, member in value.data.items():
                    values.setdefault(key, member)
        order = numpy.array([v for item in made for v in item[3]], dtype=numpy.int64)
        return _Stage(_Values(values), total, order, {1: "points", 2: "lines", 3: "triangles"}[size])

    def _tessellation(self, c, p, stage, textures, memory, images):
        from .dxbc import tessellate
        tes = p.tess_eval
        size = max(1, c.patch_vertices)
        order = stage.order[:len(stage.order) // size * size]
        patches = order.reshape(-1, size)
        count = len(patches)
        if not count:
            return _Stage(_Values({}), 0, [], "points")
        # the control points and patch values: from the control shader, or the vertices and default levels
        tcs = p.tess_control
        if tcs is not None:
            out_vertices = glsl.program_layout(tcs, "out").get("vertices") or size
            lanes = count * out_vertices
            patch_of = numpy.repeat(numpy.arange(count), out_vertices)
            rows = patches[patch_of]
            inputs = self._inputs_from(tcs, stage.rt, rows, {
                "gl_InvocationID": glsl.Val("int", numpy.tile(numpy.arange(out_vertices), count)),
                "gl_PatchVerticesIn": glsl.Val("int", numpy.full(lanes, size)),
                "gl_PrimitiveID": glsl.Val("int", patch_of.copy())})
            for name, kind, array, layout, quals in tcs.interface("out"):
                if "patch" not in quals and array == -1:
                    inputs[name] = glsl.zero(kind, lanes, tcs.structs, out_vertices)
            inputs["gl_out"] = glsl.Val("array", [glsl.Val("__block", {"gl_Position": glsl.zero("vec4", lanes),
                                                                       "gl_PointSize": glsl.Val("float", numpy.ones(lanes))})
                                                  for _ in range(out_vertices)], out_vertices)
            rt = glsl.run_stage(tcs, lanes, inputs, self._stage_uniforms(c, p, lanes), textures, memory, images)
            first = numpy.arange(count) * out_vertices
            control, patch = {}, {}
            for name, kind, array, layout, quals in tcs.interface("out"):
                if not rt.has(name):
                    continue
                value = rt.lookup(name)
                if "patch" in quals:
                    patch[name] = glsl._rows(value, first)
                elif value.kind == "array":
                    control[name] = [glsl._rows(value.data[j], first + j) for j in range(min(out_vertices, len(value.data)))]
            gl_out = rt.lookup("gl_out")
            control["gl_Position"] = [glsl._rows(gl_out.data[j].data["gl_Position"], first + j) for j in range(out_vertices)]
            outer = numpy.stack([numpy.asarray(v.data, dtype=numpy.float64)[first] for v in rt.lookup("gl_TessLevelOuter").data], 1)
            inner = numpy.stack([numpy.asarray(v.data, dtype=numpy.float64)[first] for v in rt.lookup("gl_TessLevelInner").data], 1)
        else:
            out_vertices = size
            control, patch = {}, {}
            for name, kind, array, layout, quals in tes.interface("in"):
                if "patch" not in quals and stage.rt.has(name):
                    control[name] = [glsl._rows(stage.rt.lookup(name), patches[:, j]) for j in range(size)]
            control["gl_Position"] = [glsl._rows(stage.rt.lookup("gl_Position"), patches[:, j]) for j in range(size)]
            outer = numpy.tile(numpy.array(c.patch_outer, dtype=numpy.float64), (count, 1))
            inner = numpy.tile(numpy.array(c.patch_inner, dtype=numpy.float64), (count, 1))
        # the domain points of each patch
        layout = glsl.program_layout(tes, "in")
        domain = 3 if "quads" in layout else 1 if "isolines" in layout else 2
        partitioning = 4 if "fractional_even_spacing" in layout else 3 if "fractional_odd_spacing" in layout else 1
        edges = {2: 3, 3: 4, 1: 2}[domain]
        insides = {2: 1, 3: 2, 1: 0}[domain]
        points, owners, prims = [], [], []
        total = 0
        kind = "triangles"
        for q in range(count):
            domain_points, prim, kind = tessellate(domain, list(outer[q, :edges]), list(inner[q, :insides]), partitioning)
            if not len(domain_points):
                continue
            if kind == "triangles" and "cw" in layout:
                prim = prim[:, [0, 2, 1]]
            points.append(domain_points)
            owners.append(numpy.full(len(domain_points), q))
            prims.append(prim + total)
            total += len(domain_points)
        if not total:
            return _Stage(_Values({}), 0, [], "points")
        points = numpy.concatenate(points)
        owner = numpy.concatenate(owners)
        prims = numpy.concatenate(prims)
        inputs = {"gl_TessCoord": glsl.Val("vec3", points.astype(numpy.float64)),
                  "gl_PatchVerticesIn": glsl.Val("int", numpy.full(total, out_vertices)),
                  "gl_PrimitiveID": glsl.Val("int", owner.copy()),
                  "gl_TessLevelOuter": glsl.Val("array", [glsl.Val("float", outer[owner, k]) for k in range(4)], 4),
                  "gl_TessLevelInner": glsl.Val("array", [glsl.Val("float", inner[owner, k]) for k in range(2)], 2)}
        for name, items in control.items():
            if name == "gl_Position":
                continue
            inputs[name] = glsl.Val("array", [glsl._rows(item, owner) for item in items], len(items))
        inputs["gl_in"] = glsl.Val("array", [glsl.Val("__block", {"gl_Position": glsl._rows(item, owner)})
                                             for item in control["gl_Position"]], len(control["gl_Position"]))
        for name, value in patch.items():
            inputs[name] = glsl._rows(value, owner)
        rt = glsl.run_stage(tes, total, inputs, self._stage_uniforms(c, p, total), textures, memory, images)
        if "point_mode" in layout:
            return _Stage(rt, total, numpy.arange(total), "points")
        return _Stage(rt, total, prims.reshape(-1), "lines" if kind == "lines" else "triangles")

    def _glPatchParameteri(self, c, name, value):
        if name == 0x8E72:      # GL_PATCH_VERTICES
            c.patch_vertices = max(1, int(value))

    def _glPatchParameterfv(self, c, name, values):
        count = 4 if name == 0x8E74 else 2      # GL_PATCH_DEFAULT_OUTER_LEVEL / INNER_LEVEL
        data = list(struct.unpack(f"<{count}f", bytes(self.dp.read(values, 4 * count))))
        if name == 0x8E74:
            c.patch_outer = data
        elif name == 0x8E73:
            c.patch_inner = data

    def _glDispatchCompute(self, c, x, y, z):
        p = c.program
        if p is None or p.compute is None or not (x and y and z):
            return
        memory, write_back = self._storage(c, p)
        textures = {unit: self._texture_function(c, unit) for unit in range(16)}
        try:
            glsl.run_compute(p.compute, (int(x), int(y), int(z)), self._uniform_values_for(c, p, 1), textures, memory,
                             self._images(c))
        except (glsl.GLSLError, IndexError, ValueError, KeyError) as e:
            self._missing_feature(f"GLSL: {e}")
        write_back()

    def _glDispatchComputeIndirect(self, c, offset):
        buffer = c.bindings.get(0x90EE)     # GL_DISPATCH_INDIRECT_BUFFER
        if buffer is not None:
            x, y, z = struct.unpack("<3I", bytes(self.dp.read(buffer.address + int(offset), 12)))
            self._glDispatchCompute(c, x, y, z)

    def _glMemoryBarrier(self, c, bits):
        return None

    _glMemoryBarrierByRegion = _glMemoryBarrier

    def _glBindImageTexture(self, c, unit, texture, level, layered, layer, access, fmt):
        if texture:
            c.image_units[int(unit)] = (int(texture), int(level), bool(layered), int(layer), int(access), int(fmt))
        else:
            c.image_units.pop(int(unit), None)

    def _glGetProgramResourceIndex(self, c, program, interface, name):
        p = c.programs.get(program)
        text = read_astring(self.dp, name) or ""
        if p is None:
            return 0xFFFFFFFF
        if interface == 0x92E6:     # GL_SHADER_STORAGE_BLOCK
            return p.storage_blocks.index(text) if text in p.storage_blocks else 0xFFFFFFFF
        if interface == 0x92E2:     # GL_UNIFORM_BLOCK
            return next((k for k, b in enumerate(p.blocks) if b["name"] == text), 0xFFFFFFFF)
        return 0xFFFFFFFF

    def _glShaderStorageBlockBinding(self, c, program, index, binding):
        p = c.programs.get(program)
        if p is not None and index < len(p.storage_blocks):
            p.storage_bindings[p.storage_blocks[index]] = int(binding)

    def _link_attributes(self, p: GLProgram):
        p.attributes, p.attribute_kinds = {}, {}
        if p.vertex is None:
            return
        used = set(p.attrib_bindings.values())
        free = [k for k in range(16) if k not in used]
        for name, kind, array, layout, quals in p.vertex.interface("in"):
            if name.startswith("gl_"):
                continue
            if "location" in layout and layout["location"] is not None:
                location = int(layout["location"].value[1])
            elif name in p.attrib_bindings:
                location = p.attrib_bindings[name]
            else:
                location = free.pop(0) if free else 15
            p.attributes[name] = location
            p.attribute_kinds[name] = kind

    def _link_uniforms(self, c, p: GLProgram):
        p.uniforms, p.uniform_kinds, p.locations, p.names, p.blocks = {}, {}, {}, {}, []
        location = 0
        structs = {}
        for program in p.stages():
            structs.update(program.structs)
            for item in program.globals:
                v = item.value
                if "uniform" not in v["qualifiers"]:
                    continue
                for name, array, init in v["items"]:
                    if name in p.uniforms:
                        continue
                    size = _const_size(array)
                    value = glsl.zero(v["kind"], 1, structs, size)
                    if v["kind"] in glsl.SAMPLERS and "binding" in v["layout"]:
                        value.data = int(v["layout"]["binding"].value[1])
                    elif init is not None:
                        try:
                            rt = glsl.Runtime(program, 1)
                            value = rt.coerce(rt.eval_init(init, v["kind"], numpy.ones(1, bool)), v["kind"])
                        except glsl.GLSLError:
                            pass
                    p.uniforms[name] = value
                    p.uniform_kinds[name] = (v["kind"], size)
                    location = self._enumerate(p, name, v["kind"], size, structs, location)
            for block in program.blocks:
                if "uniform" not in block["qualifiers"] or any(b["name"] == block["name"] for b in p.blocks):
                    continue
                binding = int(block["layout"]["binding"].value[1]) if "binding" in block["layout"] else 0
                p.blocks.append(dict(name=block["name"], members=block["members"], instance=block["instance"],
                                     binding=binding, structs=dict(program.structs)))

    def _enumerate(self, p, path, kind, size, structs, location):
        if size:
            for k in range(size):
                location = self._enumerate(p, f"{path}[{k}]", kind, None, structs, location)
            p.names[path] = p.names.get(f"{path}[0]")
            return location
        if kind in structs:
            for field, field_kind, field_array in structs[kind]:
                location = self._enumerate(p, f"{path}.{field}", field_kind, _const_size(field_array), structs, location)
            return location
        p.locations[location] = (path, kind)
        p.names[path] = location
        return location + 1

    def _link_outputs(self, p: GLProgram):
        p.outputs = {}
        if p.fragment is None:
            return
        index = 0
        for name, kind, array, layout, quals in p.fragment.interface("out"):
            if "location" in layout and layout["location"] is not None:
                p.outputs[name] = int(layout["location"].value[1])
            elif name in p.frag_bindings:
                p.outputs[name] = p.frag_bindings[name]
            else:
                p.outputs[name] = index
            index += 1

    def _glGetProgramiv(self, c, program, parameter, out):
        p = c.programs.get(program)
        value = 0
        if p is not None:
            value = {0x8B82: int(p.linked), 0x8B83: int(p.linked), 0x8B84: len(p.log) + 1 if p.log else 0,
                     0x8B85: len(p.shaders), 0x8B86: len(p.locations), 0x8B87: 64, 0x8B89: len(p.attributes),
                     0x8B8A: 64, 0x8A36: len(p.blocks), 0x8B80: 0}.get(parameter, 0)
            if parameter == 0x8267 and p.compute is not None:     # GL_COMPUTE_WORK_GROUP_SIZE
                layout = glsl.program_layout(p.compute, "in")
                self.dp.write(out, struct.pack("<3i", layout.get("local_size_x") or 1, layout.get("local_size_y") or 1,
                                               layout.get("local_size_z") or 1))
                return
        self.dp.write(out, struct.pack("<i", value))

    def _glGetProgramInfoLog(self, c, program, size, length, out):
        p = c.programs.get(program)
        self._info_log(p.log if p else "", size, length, out)

    def _glGetObjectParameterivARB(self, c, name, parameter, out):
        if name in c.programs:
            return self._glGetProgramiv(c, name, {0x8B81: 0x8B82}.get(parameter, parameter), out)
        return self._glGetShaderiv(c, name, parameter, out)

    def _glGetInfoLogARB(self, c, name, size, length, out):
        if name in c.programs:
            return self._glGetProgramInfoLog(c, name, size, length, out)
        return self._glGetShaderInfoLog(c, name, size, length, out)

    def _glValidateProgram(self, c, program):
        return None

    def _glUseProgram(self, c, program):
        c.program = c.programs.get(program) if program else None

    def _glUseProgramObjectARB(self, c, program):
        return self._glUseProgram(c, program)

    def _glDeleteProgram(self, c, program):
        p = c.programs.pop(program, None)
        if c.program is p:
            c.program = None

    def _glIsProgram(self, c, program):
        return int(program in c.programs)

    def _glGetAttribLocation(self, c, program, name):
        p = c.programs.get(program)
        text = read_astring(self.dp, name) or ""
        if p is None or text not in p.attributes:
            return 0xFFFFFFFF
        return p.attributes[text]

    def _glGetFragDataLocation(self, c, program, name):
        p = c.programs.get(program)
        text = read_astring(self.dp, name) or ""
        return p.outputs.get(text, 0xFFFFFFFF) if p else 0xFFFFFFFF

    def _glGetUniformLocation(self, c, program, name):
        p = c.programs.get(program)
        text = read_astring(self.dp, name) or ""
        if p is None:
            return 0xFFFFFFFF
        location = p.names.get(text)
        if location is None and text.endswith("]") is False:
            location = p.names.get(text + "[0]")
        return 0xFFFFFFFF if location is None else location

    def _glGetActiveUniform(self, c, program, index, size, length, count_out, kind_out, name_out):
        p = c.programs.get(program)
        if p is None or index not in p.locations:
            return
        path, kind = p.locations[index]
        self._info_log(path, size, length, name_out)
        if count_out:
            self.dp.write(count_out, struct.pack("<i", 1))
        if kind_out:
            self.dp.write(kind_out, struct.pack("<I", GL_TYPES.get(kind, 0x1406)))

    def _glGetActiveAttrib(self, c, program, index, size, length, count_out, kind_out, name_out):
        p = c.programs.get(program)
        if p is None:
            return
        names = sorted(p.attributes, key=lambda k: p.attributes[k])
        if index >= len(names):
            return
        self._info_log(names[index], size, length, name_out)
        if count_out:
            self.dp.write(count_out, struct.pack("<i", 1))
        if kind_out:
            self.dp.write(kind_out, struct.pack("<I", GL_TYPES.get(p.attribute_kinds[names[index]], 0x1406)))

    def _glGetUniformBlockIndex(self, c, program, name):
        p = c.programs.get(program)
        text = read_astring(self.dp, name) or ""
        if p is None:
            return 0xFFFFFFFF
        return next((k for k, b in enumerate(p.blocks) if b["name"] == text), 0xFFFFFFFF)

    def _glUniformBlockBinding(self, c, program, index, binding):
        p = c.programs.get(program)
        if p is not None and index < len(p.blocks):
            p.blocks[index]["binding"] = binding

    def _glGetActiveUniformBlockiv(self, c, program, index, name, out):
        p = c.programs.get(program)
        value = 0
        if p is not None and index < len(p.blocks):
            block = p.blocks[index]
            value = {0x8A3F: block["binding"], 0x8A40: _std140_size(block["members"], block["structs"]),
                     0x8A42: len(block["members"])}.get(name, 0)
        self.dp.write(out, struct.pack("<i", value))

    # uniforms -----------------------------------------------------------------
    def _set_uniform(self, c, location, values, kind, count=1, program=None):
        p = program or c.program
        if p is None or location in (0xFFFFFFFF, -1) or location not in p.locations:
            return
        for k in range(count):
            entry = p.locations.get(location + k)
            if entry is None:
                break
            path, declared = entry
            chunk = values[k * len(values) // count:(k + 1) * len(values) // count]
            _assign_path(p.uniforms, path, declared, chunk, kind)

    def _uniform_values(self, count, address, components, fmt):
        n = _i(count) * components
        return list(struct.unpack(f"<{n}{fmt}", bytes(self.dp.read(address, n * struct.calcsize(fmt))))) if n else []

    def _glUniformMatrix(self, c, location, count, transpose, address, columns, rows, program=None):
        values = self._uniform_values(count, address, columns * rows, "f")
        if transpose:
            out = []
            for k in range(_i(count)):
                m = numpy.array(values[k * columns * rows:(k + 1) * columns * rows]).reshape(rows, columns).T.reshape(-1)
                out.extend(m.tolist())
            values = out
        self._set_uniform(c, location, values, "mat", _i(count), program)

    def _glGetUniformfv(self, c, program, location, out):
        p = c.programs.get(program)
        if p is None or location not in p.locations:
            return
        value = _read_path(p.uniforms, p.locations[location][0])
        data = numpy.asarray(value.data, dtype=numpy.float64).reshape(-1) if isinstance(value.data, numpy.ndarray) else \
            numpy.array([value.data], dtype=numpy.float64)
        self.dp.write(out, struct.pack(f"<{len(data)}f", *data))

    def _glGetUniformiv(self, c, program, location, out):
        p = c.programs.get(program)
        if p is None or location not in p.locations:
            return
        value = _read_path(p.uniforms, p.locations[location][0])
        data = numpy.asarray(value.data).reshape(-1) if isinstance(value.data, numpy.ndarray) else numpy.array([value.data])
        self.dp.write(out, struct.pack(f"<{len(data)}i", *[int(v) for v in data]))

    # framebuffers -------------------------------------------------------------
    def _glGenFramebuffers(self, c, count, out):
        for name in self._new_names(c, count, out):
            c.framebuffers[name] = GLFramebuffer(name)

    _glCreateFramebuffers = _glGenFramebuffers

    def _glDeleteFramebuffers(self, c, count, names):
        for name in self._names(count, names):
            fb = c.framebuffers.pop(name, None)
            if c.draw_fb is fb:
                c.draw_fb = None
            if c.read_fb is fb:
                c.read_fb = None

    def _glIsFramebuffer(self, c, name):
        return int(name in c.framebuffers)

    def _glBindFramebuffer(self, c, target, name):
        fb = c.framebuffers.setdefault(name, GLFramebuffer(name)) if name else None
        if target in (GL_FRAMEBUFFER, GL_DRAW_FRAMEBUFFER):
            c.draw_fb = fb
        if target in (GL_FRAMEBUFFER, GL_READ_FRAMEBUFFER):
            c.read_fb = fb

    def _framebuffer(self, c, target):
        return c.read_fb if target == GL_READ_FRAMEBUFFER else c.draw_fb

    def _glFramebufferTexture2D(self, c, target, attachment, texture_target, texture, level):
        fb = self._framebuffer(c, target)
        if fb is None:
            return
        if not texture:
            fb.attachments.pop(attachment, None)
            return
        fb.attachments[attachment] = ("texture", c.texture_objects.setdefault(texture, GLTexture(texture, texture_target)),
                                      _i(level), self._layer_of(texture_target))

    def _glFramebufferTexture(self, c, target, attachment, texture, level):
        return self._glFramebufferTexture2D(c, target, attachment, GL_TEXTURE_2D, texture, level)

    def _glFramebufferTextureLayer(self, c, target, attachment, texture, level, layer):
        fb = self._framebuffer(c, target)
        if fb is not None and texture:
            fb.attachments[attachment] = ("texture", c.texture_objects.setdefault(texture, GLTexture(texture)), _i(level), _i(layer))

    def _glFramebufferRenderbuffer(self, c, target, attachment, renderbuffer_target, renderbuffer):
        fb = self._framebuffer(c, target)
        if fb is None:
            return
        if not renderbuffer:
            fb.attachments.pop(attachment, None)
            return
        fb.attachments[attachment] = ("renderbuffer", c.renderbuffers.setdefault(renderbuffer, GLRenderbuffer(renderbuffer)), 0, 0)

    def _glCheckFramebufferStatus(self, c, target):
        return 0x8CD5  # GL_FRAMEBUFFER_COMPLETE

    def _glDrawBuffers(self, c, count, buffers):
        values = self._names(count, buffers)
        if c.draw_fb is not None:
            c.draw_fb.draw_buffers = list(values)

    def _glDrawBuffer(self, c, buffer):
        if c.draw_fb is not None:
            c.draw_fb.draw_buffers = [buffer]

    def _glReadBuffer(self, c, buffer):
        if c.read_fb is not None:
            c.read_fb.read_buffer = buffer

    def _glGenRenderbuffers(self, c, count, out):
        for name in self._new_names(c, count, out):
            c.renderbuffers[name] = GLRenderbuffer(name)

    def _glDeleteRenderbuffers(self, c, count, names):
        for name in self._names(count, names):
            c.renderbuffers.pop(name, None)

    def _glBindRenderbuffer(self, c, target, name):
        c.renderbuffer = c.renderbuffers.setdefault(name, GLRenderbuffer(name)) if name else None

    def _glRenderbufferStorage(self, c, target, fmt, width, height):
        rb = c.renderbuffer
        if rb is None:
            return
        rb.format, rb.width, rb.height = fmt, _i(width), _i(height)
        rb.color = numpy.zeros((max(1, rb.height), max(1, rb.width), 4), dtype=numpy.float32)
        rb.depth = numpy.ones((max(1, rb.height), max(1, rb.width)), dtype=numpy.float32)

    def _glRenderbufferStorageMultisample(self, c, target, samples, fmt, width, height):
        return self._glRenderbufferStorage(c, target, fmt, width, height)

    def _attachment_color(self, entry):
        """The (h, w, 4) array of an attachment, rows bottom up (GL)."""
        kind, obj, level, layer = entry
        if kind == "renderbuffer":
            return obj.color
        array = obj.levels.get((layer, level))
        return array

    def _attachment_depth(self, c, fb):
        entry = fb.attachments.get(GL_DEPTH_ATTACHMENT) or fb.attachments.get(GL_DEPTH_STENCIL_ATTACHMENT)
        if entry is None:
            return None
        kind, obj, level, layer = entry
        if kind == "renderbuffer":
            return obj.depth
        array = obj.levels.get((layer, level))
        if array is None:
            return None
        if not hasattr(obj, "depth_view") or obj.depth_view is None or obj.depth_view.shape != array.shape[:2]:
            obj.depth_view = numpy.ones(array.shape[:2], dtype=numpy.float32)
        return obj.depth_view

    def _sync_depth_texture(self, c, fb):
        entry = fb.attachments.get(GL_DEPTH_ATTACHMENT) or fb.attachments.get(GL_DEPTH_STENCIL_ATTACHMENT)
        if entry is not None and entry[0] == "texture":
            obj = entry[1]
            array = obj.levels.get((entry[3], entry[2]))
            if array is not None and getattr(obj, "depth_view", None) is not None:
                array[..., 0] = obj.depth_view
                array[..., 1:3] = 0
                array[..., 3] = 1

    def _draw_targets(self, c, blend):
        """(targets, depth, height) of the draws: the default framebuffer or the bound framebuffer object."""
        fb = c.draw_fb
        if fb is None:
            framebuffer = c.ensure()
            return [Target(framebuffer.color, blend, srgb=False)], framebuffer.depth, framebuffer.height
        targets, height = [], None
        for buffer in fb.draw_buffers:
            entry = fb.attachments.get(buffer)
            array = self._attachment_color(entry) if entry is not None else None
            if array is None:
                targets.append(None)
                continue
            targets.append(Target(array[::-1], blend))
            height = array.shape[0]
        depth = self._attachment_depth(c, fb)
        if height is None and depth is not None:
            height = depth.shape[0]
        return targets, (depth[::-1] if depth is not None else None), height or 1

    def _glBlitFramebuffer(self, c, sx0, sy0, sx1, sy1, dx0, dy0, dx1, dy1, mask, filter_type):
        sx0, sy0, sx1, sy1, dx0, dy0, dx1, dy1 = (_i(v) for v in (sx0, sy0, sx1, sy1, dx0, dy0, dx1, dy1))
        source = self._read_array(c)
        target = self._write_array(c)
        if source is None or target is None or mask & 0x4000 == 0:
            return
        w, h = dx1 - dx0, dy1 - dy0
        if w == 0 or h == 0:
            return
        xs = numpy.clip(sx0 + ((numpy.arange(abs(w)) + 0.5) * (sx1 - sx0) / abs(w)).astype(int), 0, source.shape[1] - 1)
        ys = numpy.clip(sy0 + ((numpy.arange(abs(h)) + 0.5) * (sy1 - sy0) / abs(h)).astype(int), 0, source.shape[0] - 1)
        block = source[ys][:, xs]
        x0, y0 = min(dx0, dx1), min(dy0, dy1)
        if w < 0:
            block = block[:, ::-1]
        if h < 0:
            block = block[::-1]
        region = target[max(0, y0):max(0, y0) + block.shape[0], max(0, x0):max(0, x0) + block.shape[1]]
        part = block[:region.shape[0], :region.shape[1]]
        if region.dtype == numpy.uint8:
            region[...] = numpy.round(numpy.clip(part, 0, 1) * 255).astype(numpy.uint8)
        else:
            region[...] = part

    def _read_array(self, c):
        """The read framebuffer's color as (h, w, 4) floats, rows bottom up."""
        fb = c.read_fb
        if fb is None:
            framebuffer = c.ensure()
            return framebuffer.color[::-1].astype(numpy.float32) / 255
        entry = fb.attachments.get(fb.read_buffer) or fb.attachments.get(GL_COLOR_ATTACHMENT0)
        return self._attachment_color(entry) if entry is not None else None

    def _write_array(self, c):
        fb = c.draw_fb
        if fb is None:
            return c.ensure().color[::-1]
        entry = fb.attachments.get(fb.draw_buffers[0] if fb.draw_buffers else GL_COLOR_ATTACHMENT0)
        return self._attachment_color(entry) if entry is not None else None

    def _read_region(self, c, x, y, width, height):
        array = self._read_array(c)
        out = numpy.zeros((max(1, height), max(1, width), 4), dtype=numpy.float32)
        if array is not None:
            part = array[max(0, y):y + height, max(0, x):x + width]
            out[:part.shape[0], :part.shape[1]] = part
        return out

    # state ----------------------------------------------------------------------
    def _glBlendFuncSeparate(self, c, src, dst, src_a, dst_a):
        c.blend = [c.blend[0], BLEND_FACTORS.get(src, 2), BLEND_FACTORS.get(dst, 1), c.blend[3],
                   BLEND_FACTORS.get(src_a, 2), BLEND_FACTORS.get(dst_a, 1), c.blend[6]]

    def _glBlendEquation(self, c, mode):
        op = BLEND_EQUATIONS.get(mode, 1)
        c.blend[3] = c.blend[6] = op

    def _glBlendEquationSeparate(self, c, mode, mode_alpha):
        c.blend[3], c.blend[6] = BLEND_EQUATIONS.get(mode, 1), BLEND_EQUATIONS.get(mode_alpha, 1)

    def _glBlendColor(self, c, r, g, b, a):
        c.blend_color = (r, g, b, a)

    def _glProvokingVertex(self, c, mode):
        c.provoking = "first" if mode == 0x8E4D else "last"

    def _glPolygonMode(self, c, face, mode):
        c.polygon_mode = mode

    def _glDepthRangef(self, c, near, far):
        return self._glDepthRange(c, near, far)

    def _glClearDepthf(self, c, depth):
        c.clear_depth = depth

    def _glPrimitiveRestartIndex(self, c, index):
        c.restart_index = index

    def _glEnablei(self, c, cap, index):
        c.enabled.add(cap)

    def _glDisablei(self, c, cap, index):
        c.enabled.discard(cap)

    def _glColorMaski(self, c, index, r, g, b, a):
        self._glColorMask(c, r, g, b, a)

    def _glClearBufferfv(self, c, buffer, index, values):
        if buffer == 0x1800:    # GL_COLOR
            rgba = struct.unpack("<4f", bytes(self.dp.read(values, 16)))
            self._clear_color(c, rgba, index)
        elif buffer == 0x1801:  # GL_DEPTH
            depth = struct.unpack("<f", bytes(self.dp.read(values, 4)))[0]
            self._clear_depth(c, depth)

    def _glClearBufferfi(self, c, buffer, index, depth, stencil):
        self._clear_depth(c, depth)

    def _glClearBufferiv(self, c, buffer, index, values):
        if buffer == 0x1800:
            self._clear_color(c, struct.unpack("<4i", bytes(self.dp.read(values, 16))), index)

    _glClearBufferuiv = _glClearBufferiv

    def _clear_color(self, c, rgba, index=None):
        fb = c.draw_fb
        if fb is None:
            c.ensure().clear(rgba, None, self._clear_rect(c, c.ensure().height))
            return
        buffers = fb.draw_buffers if index is None else fb.draw_buffers[index:index + 1]
        for buffer in buffers:
            entry = fb.attachments.get(buffer)
            array = self._attachment_color(entry) if entry is not None else None
            if array is not None:
                array[...] = numpy.asarray(rgba, dtype=numpy.float32)

    def _clear_depth(self, c, depth):
        fb = c.draw_fb
        if fb is None:
            c.ensure().clear(None, depth, self._clear_rect(c, c.ensure().height))
            return
        array = self._attachment_depth(c, fb)
        if array is not None:
            array[...] = depth
            self._sync_depth_texture(c, fb)

    def _clear_rect(self, c, height):
        rect = self.scissor_rect(c, height)
        return rect

    def _glClear(self, c, mask):
        if mask & 0x4000:
            self._clear_color(c, c.clear_color)
        if mask & 0x100:
            self._clear_depth(c, c.clear_depth)

    def _glReadPixels(self, c, x, y, w, h, fmt, kind, out):
        x, y, w, h = _i(x), _i(y), _i(w), _i(h)
        if c.bindings.get(0x88EB) is not None:    # GL_PIXEL_PACK_BUFFER
            out = c.bindings[0x88EB].address + out
        if fmt == 0x1902:  # GL_DEPTH_COMPONENT
            fb = c.read_fb
            depth = c.ensure().depth[::-1] if fb is None else self._attachment_depth(c, fb)
            region = numpy.zeros((h, w, 4), dtype=numpy.float32)
            if depth is not None:
                part = depth[max(0, y):y + h, max(0, x):x + w]
                region[:part.shape[0], :part.shape[1], 0] = part
            self.dp.write(out, self._pack(c, region, fmt, 0x1406 if kind == 0x1406 else kind))
            return
        region = self._read_region(c, x, y, w, h)
        if kind in (0x1406,):
            self.dp.write(out, self._pack(c, region, fmt, kind))
        else:
            self.dp.write(out, self._pack(c, region, fmt, 0x1401))

    def _glFenceSync(self, c, condition, flags):
        return 1

    def _glClientWaitSync(self, c, sync, flags, timeout):
        return 0x911A  # GL_ALREADY_SIGNALED

    def _glWaitSync(self, c, sync, flags, timeout):
        return None

    def _glDeleteSync(self, c, sync):
        return None

    def _glIsSync(self, c, sync):
        return 1

    def _glGetSynciv(self, c, sync, name, size, length, values):
        self.dp.write(values, struct.pack("<i", 0x9119 if name == 0x9114 else 0))  # GL_SIGNALED

    def _glGenQueries(self, c, count, out):
        for name in self._new_names(c, count, out):
            c.queries[name] = [0, 0]

    def _glDeleteQueries(self, c, count, names):
        for name in self._names(count, names):
            c.queries.pop(name, None)

    def _glBeginQuery(self, c, target, name):
        c.queries.setdefault(name, [0, 0])[0] = self.fragments
        c.query_running[target] = name

    def _glEndQuery(self, c, target):
        name = c.query_running.pop(target, None)
        if name is not None:
            c.queries[name][1] = self.fragments - c.queries[name][0]

    def _glGetQueryObjectuiv(self, c, name, parameter, out):
        value = 1 if parameter == 0x8867 else c.queries.get(name, [0, 0])[1]   # GL_QUERY_RESULT_AVAILABLE
        self.dp.write(out, struct.pack("<I", value & 0xFFFFFFFF))

    _glGetQueryObjectiv = _glGetQueryObjectuiv

    def _glGetQueryObjectui64v(self, c, name, parameter, out):
        value = 1 if parameter == 0x8867 else c.queries.get(name, [0, 0])[1]
        self.dp.write(out, struct.pack("<Q", value))

    _glGetQueryObjecti64v = _glGetQueryObjectui64v

    def _glGetStringi(self, c, name, index):
        if name != 0x1F03 or index >= len(EXTENSIONS):
            return 0
        return self._string(f"ext{index}", EXTENSIONS[index])

    def _string(self, key, text):
        if key not in self.strings:
            address = self.dp.allocate(len(text) + 1)
            self.dp.write(address, text.encode() + b"\0")
            self.strings[key] = address
        return self.strings[key]

    def _glGetString(self, c, name):
        major, minor = c.version
        version = VERSION if (major, minor) <= (3, 3) else f"{major}.{minor}.0 Fathom emulator"
        # the GLSL of the context's version (3.30 for the 1.x to 3.3 contexts, 4.x0 for a 4.x one)
        language = "3.30" if (major, minor) <= (3, 3) else f"{major}.{minor}0"
        text = {0x1F00: "Fathom emulator", 0x1F01: "Fathom software rasterizer", 0x1F02: version,
                0x1F03: " ".join(EXTENSIONS) if (major, minor) < (3, 2) else "",
                0x8B8C: f"{language} Fathom emulator"}.get(name)
        if text is None:
            return 0
        return self._string(("string", name, version), text)

    def _glGetIntegerv(self, c, name, out):
        major, minor = c.version
        values = {0x0BA2: c.viewport or [0, 0, 0, 0], 0x0BA0: [c.mode], 0x0D33: [8192], 0x0D3A: [8192, 8192],
                  0x821B: [max(3, major)], 0x821C: [minor if major >= 3 else 3], 0x821D: [len(EXTENSIONS)],
                  0x8869: [16], 0x8872: [16], 0x8B4D: [16], 0x8B4C: [16], 0x8B4A: [1024], 0x8B49: [1024], 0x8DFB: [1024],
                  0x8DFD: [1024], 0x8824: [8], 0x8CDF: [8], 0x8B8D: [c.program.name if c.program else 0],
                  0x8CA6: [c.draw_fb.name if c.draw_fb else 0], 0x8CAA: [c.read_fb.name if c.read_fb else 0],
                  0x85B5: [c.vertex_array.name], 0x8894: [c.bindings[GL_ARRAY_BUFFER].name if c.bindings.get(GL_ARRAY_BUFFER) else 0],
                  0x84E0: [0x84C0 + c.active_unit], 0x8069: [c.units.get((c.active_unit, GL_TEXTURE_2D), 0)],
                  0x8A2F: [16], 0x8A30: [65536], 0x8A2B: [12], 0x8A2D: [12], 0x8DDF: [16], 0x9126: [2],
                  0x0D32: [16], 0x0D36: [16], 0x0D38: [16], 0x0B21: [1], 0x0BA4: [8], 0x80E8: [1 << 20], 0x80E9: [1 << 20],
                  0x8D57: [1], 0x84E8: [16384], 0x88FF: [2048], 0x0D50: [8]}.get(name, [0])
        self.dp.write(out, struct.pack(f"<{len(values)}i", *values))

    def _glGetFloatv(self, c, name, out):
        if name in (0x0BA6, 0x0BA7):
            m = c.stacks[GL_MODELVIEW if name == 0x0BA6 else GL_PROJECTION][-1]
            self.dp.write(out, struct.pack("<16f", *m.T.flatten()))
        elif name == 0x0C22:
            self.dp.write(out, struct.pack("<4f", *c.clear_color))
        elif name == 0x84FF:   # GL_MAX_TEXTURE_MAX_ANISOTROPY
            self.dp.write(out, struct.pack("<f", 16.0))
        elif name == 0x0BA2:
            self.dp.write(out, struct.pack("<4f", *(c.viewport or [0, 0, 0, 0])))
        else:
            self.dp.write(out, struct.pack("<f", 0.0))

    def _glGetBooleanv(self, c, name, out):
        self.dp.write(out, bytes([1 if name in c.enabled else 0]))

    def _glDebugMessageCallback(self, c, callback, user):
        return None

    def _glObjectLabel(self, c, *args):
        return None

    # drawing --------------------------------------------------------------------
    def _glDrawArrays(self, c, mode, first, count):
        first, count = _i(first), _i(count)
        if c.program is not None:
            self.draw_program(c, mode, numpy.arange(first, first + count), [0])
        else:
            self.draw(c, KINDS.get(mode, "points"), self._fixed_vertices(c, range(first, first + count)))

    def _glDrawArraysInstanced(self, c, mode, first, count, instances):
        first, count = _i(first), _i(count)
        if c.program is not None:
            self.draw_program(c, mode, numpy.arange(first, first + count), list(range(_i(instances))))

    def _glDrawArraysInstancedBaseInstance(self, c, mode, first, count, instances, base):
        if c.program is not None:
            self.draw_program(c, mode, numpy.arange(_i(first), _i(first) + _i(count)), list(range(_i(instances))), base)

    def _indices(self, c, count, kind, indices, base=0):
        fmt = {0x1401: "u1", 0x1403: "<u2", 0x1405: "<u4"}.get(kind, "<u4")
        size = numpy.dtype(fmt).itemsize
        count = _i(count)
        buffer = c.vertex_array.element_buffer
        if buffer is not None:
            data = buffer.data()[indices:indices + count * size].tobytes()
        else:
            data = bytes(self.dp.read(indices, count * size)) if indices else b""
        values = numpy.frombuffer(data.ljust(count * size, b"\0"), dtype=fmt).astype(numpy.int64)
        if GL_PRIMITIVE_RESTART in c.enabled and c.restart_index is not None:
            values = values[values != c.restart_index]
        return values + _sign(base, 32)

    def _glDrawElements(self, c, mode, count, kind, indices):
        ids = self._indices(c, count, kind, indices)
        if c.program is not None:
            self.draw_program(c, mode, ids, [0])
        else:
            self.draw(c, KINDS.get(mode, "points"), self._fixed_vertices(c, ids))

    def _glDrawRangeElements(self, c, mode, start, end, count, kind, indices):
        return self._glDrawElements(c, mode, count, kind, indices)

    def _glDrawElementsBaseVertex(self, c, mode, count, kind, indices, base):
        ids = self._indices(c, count, kind, indices, base)
        if c.program is not None:
            self.draw_program(c, mode, ids, [0])

    def _glDrawElementsInstanced(self, c, mode, count, kind, indices, instances):
        if c.program is not None:
            self.draw_program(c, mode, self._indices(c, count, kind, indices), list(range(_i(instances))))

    def _glDrawElementsInstancedBaseVertex(self, c, mode, count, kind, indices, instances, base):
        if c.program is not None:
            self.draw_program(c, mode, self._indices(c, count, kind, indices, base), list(range(_i(instances))))

    def _glMultiDrawArrays(self, c, mode, firsts, counts, draws):
        n = _i(draws)
        first = struct.unpack(f"<{n}i", bytes(self.dp.read(firsts, 4 * n))) if n else ()
        count = struct.unpack(f"<{n}i", bytes(self.dp.read(counts, 4 * n))) if n else ()
        for f, k in zip(first, count):
            self._glDrawArrays(c, mode, f, k)

    def _fixed_vertices(self, c, indices):
        """The vertices of the fixed pipeline from the arrays (glVertexPointer... in client memory or buffers)."""
        return self._array_vertices(c, [int(i) for i in indices])

    def _glVertexPointer(self, c, size, kind, stride, pointer):
        c.arrays[0x8074] = (_i(size), kind, _i(stride), pointer, c.bindings.get(GL_ARRAY_BUFFER))

    def _glColorPointer(self, c, size, kind, stride, pointer):
        c.arrays[0x8076] = (_i(size), kind, _i(stride), pointer, c.bindings.get(GL_ARRAY_BUFFER))

    def _glTexCoordPointer(self, c, size, kind, stride, pointer):
        c.arrays[0x8078] = (_i(size), kind, _i(stride), pointer, c.bindings.get(GL_ARRAY_BUFFER))

    def _glNormalPointer(self, c, kind, stride, pointer):
        c.arrays[0x8075] = (3, kind, _i(stride), pointer, c.bindings.get(GL_ARRAY_BUFFER))

    def _array(self, c, array, index):
        entry = c.arrays[array]
        size, kind, stride, pointer = entry[:4]
        buffer = entry[4] if len(entry) > 4 else None
        fmt, scale = {0x1406: ("f", 1), 0x140A: ("d", 1), 0x1404: ("i", 1), 0x1402: ("h", 1), 0x1401: ("B", 255),
                      0x1400: ("b", 127), 0x1403: ("H", 65535), 0x1405: ("I", 4294967295)}.get(kind, ("f", 1))
        width = struct.calcsize(fmt) * size
        base = (buffer.address + pointer) if buffer is not None else pointer
        values = struct.unpack(f"<{size}{fmt}", bytes(self.dp.read(base + index * (stride or width), width)))
        return [v / scale for v in values] if array == 0x8076 and scale != 1 else [float(v) for v in values]

    def draw(self, c, kind, vertices):
        """The fixed pipeline, with the texture of unit 0 (a texture object)."""
        texture = self._bound_texture(c, GL_TEXTURE_2D, 0) if GL_TEXTURE_2D in c.enabled else None
        array = texture.level(0) if texture is not None else None
        c.textures = {c.units.get((0, GL_TEXTURE_2D), 0): array} if array is not None else {}
        # the mip chain when the minifying filter uses mipmaps
        c.texture_levels = None
        if texture is not None and array is not None and texture.params.get(0x2801, 0x2702) in (0x2700, 0x2701, 0x2702, 0x2703):
            levels = [array]
            while texture.level(len(levels)) is not None:
                levels.append(texture.level(len(levels)))
            if len(levels) > 1:
                c.texture_levels = (levels, texture.params.get(0x2801, 0x2702) in (0x2702, 0x2703))
        c.bound = c.units.get((0, GL_TEXTURE_2D), 0)
        c.texture_parameters = {c.bound: dict(texture.params)} if texture is not None else {}
        if c.program is not None and vertices:
            self._missing_feature("OpenGL immediate mode with a GLSL program")
        return OpenGl._fixed_draw(self, c, kind, vertices)

    def render(self, c, kind, clip, attrs, fragment, flat=None, targets=None):
        prims = primitives(kind, len(clip))
        raster_kind = kind_of(kind)
        if raster_kind == "triangles" and GL_CULL_FACE in c.enabled and c.cull == GL_FRONT_AND_BACK:
            return
        cull = "none"
        if GL_CULL_FACE in c.enabled:
            cull = "back" if c.cull == GL_BACK else "front"
        blend = Blend(GL_BLEND in c.enabled, *c.blend[1:], write_mask=c.color_mask)
        target_list, depth, height = self._draw_targets(c, blend)
        x, y, w, h = c.viewport or [0, 0, 1, 1]
        pipeline = Pipeline((x, y, w, h), "gl", c.depth_range, cull=cull, front_cw=c.front == GL_CCW, provoking=c.provoking,
                            point_size=c.point_size, line_width=c.line_width, scissor=self.scissor_rect(c, height))
        output = Output(target_list, depth, c.depth_func if GL_DEPTH_TEST in c.enabled else 0, c.depth_mask, c.blend_color)
        output.height = height
        if target_list and target_list[0] is not None:
            output.height, output.width = target_list[0].color.shape[:2]
        elif depth is not None:
            output.height, output.width = depth.shape
        if raster_kind == "triangles" and c.polygon_mode == 0x1B01:    # GL_LINE
            prims = numpy.concatenate([prims[:, [0, 1]], prims[:, [1, 2]], prims[:, [2, 0]]])
            raster_kind = "lines"
        written = draw(output, pipeline, raster_kind, prims, attrs, fragment, clip=clip, flat_mask=flat)
        self.fragments += written
        if c.draw_fb is not None:
            self._sync_depth_texture(c, c.draw_fb)

    def draw_program(self, c, mode, vertex_ids, instances, base_instance=0):
        p = c.program
        if p is None or not p.linked or p.vertex is None:
            return
        kind = KINDS.get(mode, "points")
        if mode == 0x000E:          # GL_PATCHES
            kind = "patches"
        elif 0x000A <= mode <= 0x000D:
            kind = {0xA: "lines_adj", 0xB: "line_strip_adj", 0xC: "triangles_adj", 0xD: "strip_adj"}[mode]
        elif mode > 9:
            self._missing_feature(f"OpenGL primitive {mode}")
            return
        vertex_ids = numpy.asarray(vertex_ids, dtype=numpy.int64)
        if not len(vertex_ids):
            return
        try:
            for instance in instances:
                self._draw_instance(c, p, kind, vertex_ids, instance, base_instance)
        except (glsl.GLSLError, IndexError, ValueError, KeyError) as e:
            self._missing_feature(f"GLSL: {e}")

    def _uniform_values_for(self, c, p: GLProgram, n):
        uniforms = {}
        for name, value in p.uniforms.items():
            uniforms[name] = _broadcast_val(value, n)
        for block in p.blocks:
            entry = c.indexed_bindings.get((GL_UNIFORM_BUFFER, block["binding"]))
            data = b""
            if entry is not None and entry[0] is not None:
                buffer, offset, size = entry
                data = bytes(self.dp.read(buffer.address + offset, size or buffer.size))
            values = _std140_read(block["members"], block["structs"], data)
            if block["instance"]:
                uniforms[block["instance"]] = glsl.Val("__block", {k: _broadcast_val(v, n) for k, v in values.items()})
            else:
                for k, v in values.items():
                    uniforms[k] = _broadcast_val(v, n)
        modelview = c.stacks[GL_MODELVIEW][-1]
        projection = c.stacks[GL_PROJECTION][-1]
        mvp = projection @ modelview
        for name, matrix in (("gl_ModelViewMatrix", modelview), ("gl_ProjectionMatrix", projection),
                             ("gl_ModelViewProjectionMatrix", mvp)):
            uniforms[name] = glsl.Val("mat4", numpy.broadcast_to(matrix.T, (n, 4, 4)))
        normal = numpy.linalg.inv(modelview[:3, :3]).T if abs(numpy.linalg.det(modelview[:3, :3])) > 1e-12 else numpy.identity(3)
        uniforms["gl_NormalMatrix"] = glsl.Val("mat3", numpy.broadcast_to(normal.T, (n, 3, 3)))
        return uniforms

    def _draw_instance(self, c, p: GLProgram, kind, vertex_ids, instance, base_instance):
        unique, inverse = numpy.unique(vertex_ids, return_inverse=True)
        n = len(unique)
        inputs = {"gl_VertexID": glsl.Val("int", unique.copy()), "gl_InstanceID": glsl.Val("int", numpy.full(n, instance))}
        for name, location in p.attributes.items():
            attrib = c.vertex_array.attribs.get(location)
            if attrib is not None and attrib["enabled"]:
                rows = unique if not attrib["divisor"] else numpy.full(n, (instance // attrib["divisor"]) + base_instance)
                data = self._read_attribute(c, attrib, rows)
            else:
                data = numpy.tile(numpy.array(c.generic.get(location, [0.0, 0.0, 0.0, 1.0])), (n, 1))
            inputs[name] = _attribute_val(p.attribute_kinds[name], data, n)
        # the compatibility attributes from the arrays of the fixed pipeline
        if 0x8074 in c.arrays and 0x8074 in c.client:
            position = numpy.array([self._array(c, 0x8074, int(i)) + [0.0, 0.0, 1.0][len(self._array(c, 0x8074, int(i))) - 1:]
                                    for i in unique])[:, :4] if n else numpy.zeros((0, 4))
            if position.shape[1] < 4:
                position = numpy.concatenate([position, numpy.ones((n, 4 - position.shape[1]))], 1)
            inputs["gl_Vertex"] = glsl.Val("vec4", position)
        elif 0 in c.vertex_array.attribs and c.vertex_array.attribs[0]["enabled"]:
            inputs["gl_Vertex"] = glsl.Val("vec4", self._read_attribute(c, c.vertex_array.attribs[0], unique))
        if 0x8076 in c.arrays and 0x8076 in c.client:
            inputs["gl_Color"] = glsl.Val("vec4", numpy.array([(self._array(c, 0x8076, int(i)) + [1.0])[:4] for i in unique]))
        else:
            inputs["gl_Color"] = glsl.Val("vec4", numpy.tile(numpy.array(c.color, dtype=float), (n, 1)))
        if 0x8078 in c.arrays and 0x8078 in c.client:
            coords = numpy.array([(self._array(c, 0x8078, int(i)) + [0.0, 0.0, 1.0])[:4] for i in unique])
            inputs["gl_MultiTexCoord0"] = glsl.Val("vec4", coords)
        if 0x8075 in c.arrays and 0x8075 in c.client:
            inputs["gl_Normal"] = glsl.Val("vec3", numpy.array([self._array(c, 0x8075, int(i))[:3] for i in unique]))
        else:
            inputs["gl_Normal"] = glsl.Val("vec3", numpy.tile(numpy.array(c.normal, dtype=float), (n, 1)))
        uniforms = self._uniform_values_for(c, p, n)
        textures = {unit: self._texture_function(c, unit) for unit in range(16)}
        memory, write_back = self._storage(c, p)
        images = self._images(c)
        rt = glsl.run_vertex(p.vertex, n, inputs, uniforms, textures)
        if kind == "patches" or kind.endswith("_adj") or p.geometry is not None or p.tess_eval is not None:
            stage = _Stage(rt, n, inverse, kind)
            if p.tess_eval is not None and kind == "patches":
                stage = self._tessellation(c, p, stage, textures, memory, images)
            elif kind == "patches":
                write_back()
                return
            if p.geometry is not None and stage.n:
                stage = self._geometry(c, p, stage, textures, memory, images)
            elif kind.endswith("_adj"):
                stage.order, stage.kind = _strip_adjacency(stage.order, stage.kind)
            rt, n, inverse, kind = stage.rt, stage.n, stage.order, stage.kind
            write_back()
            if not n or not len(inverse):
                return
        else:
            write_back()
        clip = numpy.asarray(rt.lookup("gl_Position").data, dtype=numpy.float64).reshape(n, 4)
        # the varyings the fragment shader reads
        links, columns, flat = [], [], []
        fragment_program = p.fragment
        if fragment_program is not None:
            wanted = [(name, kind_, array, layout, quals) for name, kind_, array, layout, quals in fragment_program.interface("in")]
            sources = {}
            for name, kind_, array, layout, quals in wanted:
                value = None
                try:
                    value = rt.lookup(name)
                except glsl.GLSLError:
                    value = None
                if value is None:
                    continue
                sources[name] = (value, kind_, "flat" in quals or glsl.base_of(kind_) in ("int", "uint"))
            fragment_source = fragment_program_source(p)
            for compat, source in (("gl_Color", "gl_FrontColor"), ("gl_SecondaryColor", "gl_FrontSecondaryColor")):
                if compat in fragment_source:
                    try:
                        sources[compat] = (rt.lookup(source), "vec4", False)
                    except glsl.GLSLError:
                        pass
            try:
                texcoords = rt.lookup("gl_TexCoord")
                for k, item in enumerate(texcoords.data):
                    sources[f"gl_TexCoord[{k}]"] = (item, "vec4", False)
            except glsl.GLSLError:
                pass
            for name, (value, kind_, is_flat) in sources.items():
                parts = _flatten(value, n)
                if parts is None:
                    continue
                links.append((name, value.kind, len(columns) and sum(col.shape[1] for col in columns), parts.shape[1],
                              value))
                columns.append(parts)
                flat.extend([is_flat] * parts.shape[1])
        attrs = numpy.concatenate(columns, axis=1) if columns else numpy.zeros((n, 0))
        flat_mask = numpy.array(flat, dtype=bool) if flat else None
        prims_kind = kind
        if prims_kind == "points":
            size = rt.lookup("gl_PointSize").data
            c.point_size = float(numpy.asarray(size).reshape(-1)[0]) if 0x8642 in c.enabled and len(size) else c.point_size
        clip_all = clip[inverse]
        attrs_all = attrs[inverse]
        writes_depth = "gl_FragDepth" in fragment_program_source(p)
        outputs = sorted(p.outputs.items(), key=lambda kv: kv[1])
        source = fragment_program_source(p)

        def fragment(frags):
            if fragment_program is None:
                return [None], None, None
            inputs_f = {}
            for name, kind_, start, width, template in links:
                values = frags.attrs[:, start:start + width]
                value = _unflatten(template, values, frags.n)
                if name.startswith("gl_TexCoord["):
                    continue
                inputs_f[name] = value
            texcoord_items = [_unflatten(t, frags.attrs[:, s:s + w], frags.n) if name.startswith("gl_TexCoord[") else None
                              for name, kind_, s, w, t in links]
            items = [v for v, link in zip(texcoord_items, links) if link[0].startswith("gl_TexCoord[")]
            if items:
                inputs_f["gl_TexCoord"] = glsl.Val("array", items, len(items))
            height = target_height(c)
            inputs_f["gl_FragCoord"] = glsl.Val("vec4", numpy.stack([frags.x + 0.5, height - frags.y - 0.5, frags.z,
                                                                     frags.inv_w], 1))
            inputs_f["gl_FrontFacing"] = glsl.Val("bool", frags.front.copy())
            inputs_f["gl_PointCoord"] = glsl.Val("vec2", numpy.zeros((frags.n, 2)))
            inputs_f["gl_PrimitiveID"] = glsl.Val("int", frags.prim.astype(numpy.int64))
            inputs_f["gl_FragDepth"] = glsl.Val("float", numpy.asarray(frags.z, dtype=numpy.float64).copy())
            uniforms_f = self._uniform_values_for(c, p, frags.n)
            rt_f = glsl.run_fragment(fragment_program, frags.n, inputs_f, uniforms_f, textures)
            colors = []
            targets_count = len(c.draw_fb.draw_buffers) if c.draw_fb is not None else 1
            if outputs:
                by_location = {}
                for name, location in outputs:
                    value = rt_f.lookup(name)
                    if value.kind == "array":
                        for k, item in enumerate(value.data):
                            by_location[location + k] = item
                    else:
                        by_location[location] = value
                for k in range(targets_count):
                    value = by_location.get(k)
                    colors.append(_rgba(value, frags.n) if value is not None else None)
            else:
                data = rt_f.lookup("gl_FragData")
                written = rt_f.lookup("gl_FragColor")
                uses_data = "gl_FragData" in fragment_program_source(p)
                for k in range(targets_count):
                    source = data.data[k] if uses_data else written
                    colors.append(_rgba(source, frags.n))
            depth = None
            if writes_depth:
                depth = numpy.asarray(rt_f.lookup("gl_FragDepth").data, dtype=numpy.float64).reshape(frags.n) \
                    if rt_f.has("gl_FragDepth") else None
            return colors, ~rt_f.discarded, depth

        # the fragments in 2x2 quads for the derivatives (dFdx...) and the mip levels of mipmapped textures
        fragment.quads = any(word in source for word in ("dFdx", "dFdy", "fwidth")) or \
            ("texture" in source and self._mipmapped_units(c))
        self.render(c, kind, clip_all, attrs_all, fragment, flat_mask)

    def _mipmapped_units(self, c) -> bool:
        for (unit, target), name in c.units.items():
            texture = c.texture_objects.get(name)
            if texture is not None and texture.level(1) is not None and \
                    texture.params.get(0x2801, 0x2702) in (0x2700, 0x2701, 0x2702, 0x2703):
                return True
        return False


def fragment_program_source(p: GLProgram) -> str:
    return "".join(s.source for s in p.shaders if s.kind == "fragment")


def target_height(c) -> int:
    fb = c.draw_fb
    if fb is None:
        return c.ensure().height
    for buffer in fb.draw_buffers:
        entry = fb.attachments.get(buffer)
        if entry is not None:
            kind, obj, level, layer = entry
            array = obj.color if kind == "renderbuffer" else obj.levels.get((layer, level))
            if array is not None:
                return array.shape[0]
    return 1


GL_PRIMITIVE_RESTART = 0x8F9D
BLEND_EQUATIONS = {0x8006: 1, 0x800A: 2, 0x800B: 3, 0x8007: 4, 0x8008: 5}
GL_TYPES = {"float": 0x1406, "vec2": 0x8B50, "vec3": 0x8B51, "vec4": 0x8B52, "int": 0x1404, "ivec2": 0x8B53,
            "ivec3": 0x8B54, "ivec4": 0x8B55, "bool": 0x8B56, "uint": 0x1405, "mat2": 0x8B5A, "mat3": 0x8B5B, "mat4": 0x8B5C,
            "sampler2D": 0x8B5E, "samplerCube": 0x8B60, "sampler3D": 0x8B5F}


# ------------------------------------------------------------------ helpers
def _const_size(node):
    if node is None or node == -1:
        return None
    if hasattr(node, "op") and node.op == "const":
        return int(node.value[1])
    return None


class _Stage:
    """The vertices the last stage left (a runtime or what a geometry shader emitted) and their primitives:
    order (the vertices of the primitives, in sequence) and kind."""

    def __init__(self, rt, n, order, kind):
        self.rt, self.n, self.order, self.kind = rt, n, numpy.asarray(order, dtype=numpy.int64), kind


class _Values:
    """A runtime-like set of values (lookup by name)."""

    def __init__(self, values):
        self.values = values

    def lookup(self, name):
        if name not in self.values:
            raise glsl.GLSLError(f"unknown variable {name}")
        return self.values[name]

    def has(self, name):
        return name in self.values


def _assemble(order, kind):
    """The primitives (p, k) of a sequence of vertices: rows of vertex indices."""
    order = numpy.asarray(order, dtype=numpy.int64)
    count = len(order)
    if kind == "lines_adj":
        return order[:count // 4 * 4].reshape(-1, 4), "lines_adj"
    if kind == "line_strip_adj":
        n = max(0, count - 3)
        return order[numpy.stack([numpy.arange(n) + k for k in range(4)], 1)] if n else numpy.zeros((0, 4), numpy.int64), "lines_adj"
    if kind == "triangles_adj":
        return order[:count // 6 * 6].reshape(-1, 6), "triangles_adj"
    if kind == "strip_adj":
        rows = []
        for t in range(max(0, (count - 4) // 2)):
            a, b, c = 2 * t, 2 * t + 2, 2 * t + 4
            rows.append((a, a + 1, b, min(c + 1, count - 1), c, b + 1) if t % 2 == 0 else (b, a + 1, a, min(c + 1, count - 1), c, b + 1))
        return (order[numpy.array(rows)] if rows else numpy.zeros((0, 6), numpy.int64)), "triangles_adj"
    prims = primitives(kind, count)
    simple = kind_of(kind)
    return (order[prims] if len(prims) else numpy.zeros((0, {"points": 1, "lines": 2}.get(simple, 3)), numpy.int64)), simple


def _strip_adjacency(order, kind):
    prims, simple = _assemble(order, kind)
    if simple == "lines_adj":
        return prims[:, [1, 2]].reshape(-1), "lines"
    return prims[:, [0, 2, 4]].reshape(-1), "triangles"


def _concat(values):
    """Values of several lanes sets, one after the other."""
    first = values[0]
    if first.kind == "array":
        return glsl.Val("array", [_concat([v.data[k] for v in values]) for k in range(len(first.data))], first.length)
    if isinstance(first.data, dict):
        return glsl.Val(first.kind, {k: _concat([v.data[k] for v in values]) for k in first.data})
    if isinstance(first.data, numpy.ndarray):
        return glsl.Val(first.kind, numpy.concatenate([v.data for v in values]))
    return first


def _per_vertex(value, rows):
    """An array input of the next stage: item k, the value of vertex rows[:, k]."""
    return glsl.Val("array", [glsl._rows(value, rows[:, k]) for k in range(rows.shape[1])], rows.shape[1])


def _broadcast_val(value: glsl.Val, n: int) -> glsl.Val:
    if value.kind == "array":
        return glsl.Val("array", [_broadcast_val(v, n) for v in value.data], value.length)
    if isinstance(value.data, dict):
        return glsl.Val(value.kind, {k: _broadcast_val(v, n) for k, v in value.data.items()})
    if not isinstance(value.data, numpy.ndarray):
        return value
    return glsl.Val(value.kind, numpy.broadcast_to(value.data[:1], (n,) + value.data.shape[1:]))


def _attribute_val(kind: str, data: numpy.ndarray, n: int) -> glsl.Val:
    if kind in glsl.MATRICES:
        c, r = glsl.MATRICES[kind]
        return glsl.Val(kind, numpy.zeros((n, c, r)))
    size = glsl.size_of(kind)
    base = glsl.base_of(kind)
    values = data[:, :size]
    if base in ("int", "uint"):
        values = numpy.round(values).astype(numpy.int64)
    elif base == "bool":
        values = values != 0
    return glsl.Val(kind, values[:, 0] if size == 1 else values)


def _flatten(value: glsl.Val, n: int):
    if value.kind == "array" or isinstance(value.data, dict) or not isinstance(value.data, numpy.ndarray):
        return None
    data = value.data.astype(numpy.float64)
    return data.reshape(n, -1)


def _unflatten(template: glsl.Val, values: numpy.ndarray, n: int) -> glsl.Val:
    kind = template.kind
    base = glsl.base_of(kind)
    if kind in glsl.MATRICES:
        c, r = glsl.MATRICES[kind]
        return glsl.Val(kind, values.reshape(n, c, r))
    if base in ("int", "uint"):
        values = numpy.round(values).astype(numpy.int64)
    elif base == "bool":
        values = values != 0
    return glsl.Val(kind, values[:, 0] if values.shape[1] == 1 else values)


def _rgba(value: glsl.Val, n: int) -> numpy.ndarray:
    data = numpy.asarray(value.data, dtype=numpy.float64)
    data = data.reshape(n, -1) if data.ndim > 1 else data[:, None]
    out = numpy.zeros((n, 4))
    out[:, 3] = 1
    out[:, :min(4, data.shape[1])] = data[:, :4]
    if data.shape[1] == 1:
        out[:, 1:3] = 0
    return out


def _assign_path(uniforms, path, declared, values, kind):
    """A uniform (or an element / field of one) from glUniform* values."""
    import re as _re
    parts = _re.findall(r"(\w+)|\[(\d+)\]", path)
    name = parts[0][0]
    if name not in uniforms:
        return
    target = uniforms[name]
    chain = [target]
    for field, index in parts[1:]:
        current = chain[-1]
        if index:
            current = current.data[int(index)]
        else:
            current = current.data[field]
        chain.append(current)
    leaf = chain[-1]
    if leaf.kind in glsl.SAMPLERS:
        leaf.data = int(values[0]) if values else 0
        return
    if not isinstance(leaf.data, numpy.ndarray):
        return
    flat = numpy.array(values, dtype=numpy.float64)
    count = leaf.data[0].size
    flat = numpy.resize(flat, count) if len(flat) != count else flat
    if leaf.data.dtype == bool:
        leaf.data[0] = flat.reshape(leaf.data[0].shape) != 0
    elif leaf.data.dtype.kind == "i":
        leaf.data[0] = numpy.round(flat).astype(numpy.int64).reshape(leaf.data[0].shape)
    else:
        leaf.data[0] = flat.reshape(leaf.data[0].shape)


def _read_path(uniforms, path):
    import re as _re
    parts = _re.findall(r"(\w+)|\[(\d+)\]", path)
    current = uniforms[parts[0][0]]
    for field, index in parts[1:]:
        current = current.data[int(index)] if index else current.data[field]
    return current


# std140 ----------------------------------------------------------------------
def _std140_layout(kind, structs):
    """(size, alignment) of a type in std140."""
    if kind in structs:
        offset = 0
        for field, field_kind, field_array in structs[kind]:
            size, align = _std140_member(field_kind, _const_size(field_array), structs)
            offset = (offset + align - 1) // align * align + size
        return (offset + 15) // 16 * 16, 16
    if kind in glsl.MATRICES:
        c, r = glsl.MATRICES[kind]
        return 16 * c, 16
    count = glsl.size_of(kind)
    if count == 1:
        return 4, 4
    if count == 2:
        return 8, 8
    return 4 * count, 16


def _std140_member(kind, array, structs):
    size, align = _std140_layout(kind, structs)
    if array:
        stride = (size + 15) // 16 * 16
        return stride * array, 16
    return size, align


def _std140_size(members, structs):
    offset = 0
    for name, kind, array, layout in members:
        size, align = _std140_member(kind, _const_size(array), structs)
        offset = (offset + align - 1) // align * align + size
    return (offset + 15) // 16 * 16


def _std140_value(kind, structs, data: bytes, offset: int):
    if kind in structs:
        fields, cursor = {}, offset
        for field, field_kind, field_array in structs[kind]:
            size, align = _std140_member(field_kind, _const_size(field_array), structs)
            cursor = (cursor + align - 1) // align * align
            fields[field] = _std140_read_one(field_kind, _const_size(field_array), structs, data, cursor)
            cursor += size
        return glsl.Val(kind, fields)
    if kind in glsl.MATRICES:
        c, r = glsl.MATRICES[kind]
        values = numpy.zeros((1, c, r))
        for column in range(c):
            chunk = data[offset + 16 * column:offset + 16 * column + 4 * r].ljust(4 * r, b"\0")
            values[0, column] = struct.unpack(f"<{r}f", chunk)
        return glsl.Val(kind, values)
    count = glsl.size_of(kind)
    base = glsl.base_of(kind)
    fmt = {"float": "f", "int": "i", "uint": "I", "bool": "I"}[base]
    chunk = data[offset:offset + 4 * count].ljust(4 * count, b"\0")
    values = numpy.array(struct.unpack(f"<{count}{fmt}", chunk))
    if base == "bool":
        values = values != 0
    elif base == "float":
        values = values.astype(numpy.float64)
    else:
        values = values.astype(numpy.int64)
    return glsl.Val(kind, values.reshape(1, count) if count > 1 else values.reshape(1))


def _std140_read_one(kind, array, structs, data, offset):
    if array:
        size, _ = _std140_layout(kind, structs)
        stride = (size + 15) // 16 * 16
        return glsl.Val("array", [_std140_value(kind, structs, data, offset + stride * k) for k in range(array)], array)
    return _std140_value(kind, structs, data, offset)


def _std140_read(members, structs, data: bytes):
    values, offset = {}, 0
    for name, kind, array, layout in members:
        size, align = _std140_member(kind, _const_size(array), structs)
        offset = (offset + align - 1) // align * align
        values[name] = _std140_read_one(kind, _const_size(array), structs, data, offset)
        offset += size
    return values


# ------------------------------------------------------------------ installation
def _uniform_setters():
    """glUniform{1234}{f,i,ui}[v], glUniformMatrix*fv and glProgramUniform* as methods."""
    for count in (1, 2, 3, 4):
        for suffix, fmt in (("f", "f"), ("i", "i"), ("ui", "I")):
            setattr(GLMore, f"_glUniform{count}{suffix}",
                    (lambda kind: lambda self, c, location, *values: self._set_uniform(
                        c, location, [_sign(v, 32) if kind == "i" and isinstance(v, int) else v for v in values], kind))(suffix))
            setattr(GLMore, f"_glUniform{count}{suffix}v",
                    (lambda n, f, kind: lambda self, c, location, k, address: self._set_uniform(
                        c, location, self._uniform_values(k, address, n, f), kind, _i(k)))(count, fmt, suffix))
            setattr(GLMore, f"_glProgramUniform{count}{suffix}",
                    (lambda kind: lambda self, c, program, location, *values: self._set_uniform(
                        c, location, list(values), kind, 1, c.programs.get(program)))(suffix))
            setattr(GLMore, f"_glProgramUniform{count}{suffix}v",
                    (lambda n, f, kind: lambda self, c, program, location, k, address: self._set_uniform(
                        c, location, self._uniform_values(k, address, n, f), kind, _i(k), c.programs.get(program)))(count, fmt, suffix))
    for columns in (2, 3, 4):
        for rows in (2, 3, 4):
            name = f"{columns}" if columns == rows else f"{columns}x{rows}"
            setattr(GLMore, f"_glUniformMatrix{name}fv",
                    (lambda c_, r_: lambda self, c, location, count, transpose, address: self._glUniformMatrix(
                        c, location, count, transpose, address, c_, r_))(columns, rows))
            setattr(GLMore, f"_glProgramUniformMatrix{name}fv",
                    (lambda c_, r_: lambda self, c, program, location, count, transpose, address: self._glUniformMatrix(
                        c, location, count, transpose, address, c_, r_, c.programs.get(program)))(columns, rows))
    for count in (1, 2, 3, 4):
        for suffix, fmt, conv in (("f", "f", float), ("d", "d", float), ("s", "h", float), ("i", "i", float)):
            setattr(GLMore, f"_glVertexAttrib{count}{suffix}",
                    (lambda self, c, index, *values: self._set_generic(c, index, [float(_sign(v, 32)) if isinstance(v, int)
                                                                                 else v for v in values])))
            setattr(GLMore, f"_glVertexAttrib{count}{suffix}v",
                    (lambda n, f: lambda self, c, index, address: self._set_generic(c, index, list(struct.unpack(
                        f"<{n}{f}", bytes(self.dp.read(address, n * struct.calcsize(f)))))))(count, fmt))
    setattr(GLMore, "_glVertexAttrib4Nub", lambda self, c, index, *values: self._set_generic(c, index, [v / 255 for v in values]))


_uniform_setters()


def _missing_feature(self, key):
    missing = self.dp._fathom_unimplemented
    if key not in missing:
        self.dp.info(f"{key} (not emulated)")
    missing[key] = missing.get(key, 0) + 1


def install(gl: OpenGl):
    """Mixes the methods into the OpenGL of a process and replaces wglGetProcAddress."""
    OpenGl._fixed_draw = OpenGl.__dict__.get("_fixed_draw", OpenGl.draw)
    for name, value in GLMore.__dict__.items():
        if name.startswith("_") and not name.startswith("__") and value is not None or name in ("draw", "render", "draw_program",
                                                                                                "sample_texture"):
            setattr(OpenGl, name, value)
    OpenGl._missing_feature = _missing_feature
    gl.fragments = 0
    gl.stubs: Dict[str, int] = {}
    dp = gl.dp

    def get_proc_address(dp, address):
        name = read_astring(dp, address) or ""
        if name in gl.stubs:
            return gl.stubs[name]
        signature = OPENGL_EXT[name] if name in OPENGL_EXT else WGL_EXT.get(name)
        if signature is None:
            missing = dp._fathom_unimplemented
            key = f"OpenGL extension {name}"
            missing[key] = missing.get(key, 0) + 1
            return 0
        handler = _wgl_handler(gl, name) if name.startswith("wgl") else _gl_handler(gl, name, signature)
        stub = dp.allocate(16)
        dp.write(stub, b"\xC3" * 16)
        slots = signature_slots(signature, dp._x64)
        dp.apihooks.hook_address(stub, name, slots, lambda dp, *raw, h=handler, s=signature: h(dp, *read_args(dp, s)))
        gl.stubs[name] = stub
        return stub
    dp.apihooks.register_signature("opengl32.dll", "wglGetProcAddress", "p", get_proc_address)


def _core_name(gl, name):
    if hasattr(gl, "_" + name):
        return name
    for suffix in SUFFIXES:
        if name.endswith(suffix) and hasattr(gl, "_" + name[:-len(suffix)]):
            return name[:-len(suffix)]
    return None


def _gl_handler(gl, name, signature):
    core = _core_name(gl, name)

    def handler(dp, *values):
        if core is None:
            missing = dp._fathom_unimplemented
            if name not in missing:
                dp.info(f"OpenGL {name} is not emulated (no effect)")
            missing[name] = missing.get(name, 0) + 1
            return 0
        return gl.call(core, *values)
    return handler


def _wgl_handler(gl, name):
    def create_context(dp, hdc, share, attribs):
        major, minor = 1, 1
        if attribs:
            k = 0
            while k < 64:
                key, value = struct.unpack("<2i", bytes(dp.read(attribs + 8 * k, 8)))
                if key == 0:
                    break
                if key == 0x2091:
                    major = value
                elif key == 0x2092:
                    minor = value
                k += 1
        handle = gl.create_context(dp, hdc)
        gl.contexts[handle].version = (major, minor)
        return handle

    def choose(dp, hdc, iattribs, fattribs, maximum, formats, count):
        if formats and maximum:
            dp.write(formats, struct.pack("<i", 1))
        if count:
            dp.write(count, struct.pack("<I", 1))
        return 1

    def attribs(dp, hdc, pixel_format, layer, count, names, values):
        out = []
        for k in range(_i(count)):
            key = struct.unpack("<i", bytes(dp.read(names + 4 * k, 4)))[0]
            out.append({0x2000: 1, 0x2001: 1, 0x2010: 1, 0x2011: 1, 0x2003: 0x2027, 0x2013: 0x202B, 0x2014: 32,
                        0x2015: 8, 0x2017: 8, 0x2019: 8, 0x201B: 8, 0x2022: 24, 0x2023: 8, 0x20A9: 1}.get(key, 0))
        dp.write(values, struct.pack(f"<{len(out)}i", *out))
        return 1

    def extensions(dp, *args):
        key = "wgl_extensions"
        if key not in gl.strings:
            address = dp.allocate(len(WGL_EXTENSIONS) + 1)
            dp.write(address, WGL_EXTENSIONS.encode() + b"\0")
            gl.strings[key] = address
        return gl.strings[key]

    def swap_interval(dp, interval):
        context = gl.ctx()
        if context is not None:
            context.swap_interval = interval
        return 1

    table = {"wglCreateContextAttribsARB": create_context, "wglChoosePixelFormatARB": choose,
             "wglChoosePixelFormatEXT": choose, "wglGetPixelFormatAttribivARB": attribs,
             "wglGetExtensionsStringARB": extensions, "wglGetExtensionsStringEXT": extensions,
             "wglSwapIntervalEXT": swap_interval,
             "wglGetSwapIntervalEXT": lambda dp: (gl.ctx().swap_interval if gl.ctx() else 1),
             "wglMakeContextCurrentARB": lambda dp, draw_dc, read_dc, context: gl.make_current(dp, draw_dc, context)}
    handler = table.get(name)
    if handler is None:
        def unknown(dp, *values):
            missing = dp._fathom_unimplemented
            missing[name] = missing.get(name, 0) + 1
            return 0
        return unknown
    return handler
