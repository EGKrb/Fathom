# Fathom addition to dumpulator: OpenGL 1.x for the emulated program, drawn by raster.py. Boost
# Software License 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# opengl32.dll needs a driver (ICD) the emulator does not have: its functions are implemented here.
# Contexts (wglCreateContext, wglMakeCurrent, wglDeleteContext, wglGetCurrent*), pixel formats
# (ChoosePixelFormat, SetPixelFormat, DescribePixelFormat), SwapBuffers; the matrices (glMatrixMode,
# glLoadIdentity, glOrtho, glFrustum, gluPerspective, gluLookAt, gluOrtho2D, glTranslate, glRotate,
# glScale, glLoadMatrix, glMultMatrix, glPushMatrix / glPopMatrix), glViewport; glBegin / glEnd with
# every primitive, glVertex and glColor in their forms, glRect; glClearColor, glClearDepth, glClear;
# the depth test, back-face culling, blending, flat or smooth shading, point size and line width;
# 2D textures (glGenTextures, glBindTexture, glTexImage2D, glTexSubImage2D, glTexParameter, glTexEnv
# MODULATE / REPLACE, glTexCoord), vertex arrays (glVertexPointer, glColorPointer, glTexCoordPointer,
# glDrawArrays, glDrawElements); glReadPixels, glGetString, glGetIntegerv, glGetFloatv, glGetError. What
# a frame shows goes to the window's image (gui_draw.py) at SwapBuffers / glFlush / glFinish. The other
# gl functions (lighting, display lists, fog, stencil, extensions...) do nothing and are named in the
# summary.

import math
import struct
from typing import Dict, List, Optional

from .apihooks import read_astring
from .graphics_tables import OPENGL
from .raster import Blend, Framebuffer, Output, Pipeline, Target, draw, kind_of, numpy, primitives, sample

GL_POINTS, GL_LINES, GL_LINE_LOOP, GL_LINE_STRIP, GL_TRIANGLES, GL_TRIANGLE_STRIP, GL_TRIANGLE_FAN, GL_QUADS, \
    GL_QUAD_STRIP, GL_POLYGON = range(10)
KINDS = {0: "points", 1: "lines", 2: "line_loop", 3: "line_strip", 4: "triangles", 5: "strip", 6: "fan", 7: "quads",
         8: "quad_strip", 9: "polygon"}
GL_MODELVIEW, GL_PROJECTION, GL_TEXTURE = 0x1700, 0x1701, 0x1702
GL_COLOR_BUFFER_BIT, GL_DEPTH_BUFFER_BIT = 0x4000, 0x100
GL_DEPTH_TEST, GL_CULL_FACE, GL_BLEND = 0x0B71, 0x0B44, 0x0BE2
GL_CW, GL_CCW, GL_FRONT, GL_BACK, GL_FRONT_AND_BACK = 0x0900, 0x0901, 0x0404, 0x0405, 0x0408
GL_FLAT = 0x1D00
GL_TEXTURE_2D = 0x0DE1
GL_SCISSOR_TEST = 0x0C11
# glBlendFunc factors -> Direct3D's numbers (raster.Blend)
BLEND_FACTORS = {0: 1, 1: 2, 0x300: 3, 0x301: 4, 0x302: 5, 0x303: 6, 0x304: 7, 0x305: 8, 0x306: 9, 0x307: 10, 0x308: 11,
                 0x8001: 14, 0x8002: 15, 0x8003: 14, 0x8004: 15}


class Context:
    def __init__(self, gl, hdc: int):
        self.gl = gl
        self.hdc = hdc
        self.framebuffer: Optional[Framebuffer] = None
        self.clear_color = [0.0, 0.0, 0.0, 0.0]
        self.clear_depth = 1.0
        self.viewport = None
        self.mode = GL_MODELVIEW
        self.stacks = {m: [numpy.identity(4)] for m in (GL_MODELVIEW, GL_PROJECTION, GL_TEXTURE)}
        self.color = [1.0, 1.0, 1.0, 1.0]
        self.begin: Optional[int] = None
        self.vertices: List[tuple] = []
        self.enabled = set()
        self.flat = False
        self.front = GL_CCW
        self.cull = GL_BACK
        self.point_size = 1.0
        self.line_width = 1.0
        self.pack_alignment = 4
        self.unpack_alignment = 4
        self.error = 0
        self.texcoord = [0.0, 0.0]
        self.textures: Dict[int, object] = {}     # name -> (h, w, 4) floats
        self.texture_parameters: Dict[int, dict] = {}
        self.bound = 0
        self.next_texture = 1
        self.env_mode = 0x2100  # GL_MODULATE
        self.arrays = {}         # GL_VERTEX_ARRAY... -> (size, type, stride, pointer)
        self.client = set()
        self.blend = [True, 2, 1, 1, 2, 1, 1]   # (unused), src, dst, op, src alpha, dst alpha, op alpha (Direct3D numbers)
        self.blend_color = (0.0, 0.0, 0.0, 0.0)
        self.depth_func = 2      # GL_LESS (Direct3D's number)
        self.depth_mask = True
        self.color_mask = 0xF
        self.depth_range = (0.0, 1.0)
        self.scissor = None

    @property
    def matrix(self):
        return self.stacks[self.mode][-1]

    @matrix.setter
    def matrix(self, value):
        self.stacks[self.mode][-1] = value

    def ensure(self):
        """The framebuffer the size of the window of the DC (at the first use)."""
        if self.framebuffer is None:
            drawing = self.gl.dp.gui.drawing
            dc = drawing.dcs.get(self.hdc)
            canvas = drawing.canvas(dc.hwnd) if dc is not None and dc.hwnd else None
            width, height = canvas.size if canvas is not None else (640, 480)
            self.framebuffer = Framebuffer(width, height)
            if self.viewport is None:
                self.viewport = [0, 0, width, height]
        return self.framebuffer


def _ortho(l, r, b, t, n, f):
    m = numpy.identity(4)
    m[0, 0], m[1, 1], m[2, 2] = 2 / (r - l or 1), 2 / (t - b or 1), -2 / (f - n or 1)
    m[0, 3], m[1, 3], m[2, 3] = -(r + l) / (r - l or 1), -(t + b) / (t - b or 1), -(f + n) / (f - n or 1)
    return m


def _frustum(l, r, b, t, n, f):
    m = numpy.zeros((4, 4))
    m[0, 0], m[1, 1] = 2 * n / (r - l or 1), 2 * n / (t - b or 1)
    m[0, 2], m[1, 2], m[2, 2], m[3, 2] = (r + l) / (r - l or 1), (t + b) / (t - b or 1), -(f + n) / (f - n or 1), -1
    m[2, 3] = -2 * f * n / (f - n or 1)
    return m


def _rotate(angle, x, y, z):
    length = math.sqrt(x * x + y * y + z * z) or 1.0
    x, y, z = x / length, y / length, z / length
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    m = numpy.identity(4)
    m[:3, :3] = [[x * x * (1 - c) + c, x * y * (1 - c) - z * s, x * z * (1 - c) + y * s],
                 [y * x * (1 - c) + z * s, y * y * (1 - c) + c, y * z * (1 - c) - x * s],
                 [x * z * (1 - c) - y * s, y * z * (1 - c) + x * s, z * z * (1 - c) + c]]
    return m


class OpenGl:
    def __init__(self, dp):
        self.dp = dp
        self.contexts: Dict[int, Context] = {}
        self.current: Dict[int, int] = {}     # thread -> context handle
        self.strings: Dict[int, int] = {}
        self.events: List[dict] = []

    def ctx(self) -> Optional[Context]:
        return self.contexts.get(self.current.get(self.dp.thread_id, 0))

    # ------------------------------------------------------------------ wgl, pixel formats
    def create_context(self, dp, hdc):
        handle = dp.allocate(16)
        self.contexts[handle] = Context(self, hdc)
        self.events.append(dict(kind="opengl_context", hdc=hex(hdc)))
        return handle

    def make_current(self, dp, hdc, handle):
        if not handle:
            self.current.pop(dp.thread_id, None)
            return 1
        context = self.contexts.get(handle)
        if context is None:
            return 0
        context.hdc = hdc or context.hdc
        context.ensure()
        self.current[dp.thread_id] = handle
        return 1

    def delete_context(self, dp, handle):
        if self.current.get(dp.thread_id) == handle:
            self.current.pop(dp.thread_id, None)
        return int(self.contexts.pop(handle, None) is not None)

    def describe(self, dp, hdc, index, size, pfd):
        """PIXELFORMATDESCRIPTOR of the only format: RGBA 32 bits, depth 24, double-buffered."""
        if pfd and size >= 40:
            dp.write(pfd, struct.pack("<HHIBBBBBBBBBBBBBBBBBBBBIII", 40, 1, 0x25, 0, 32, 8, 16, 8, 8, 8, 0, 8, 24, 0, 0, 0, 0, 0,
                                      24, 8, 0, 0, 0, 0, 0, 0))
        return 1

    def present(self, context: Context):
        """The frame in the window's image."""
        drawing = self.dp.gui.drawing
        dc = drawing.dcs.get(context.hdc)
        if dc is None or context.framebuffer is None:
            return
        canvas = drawing.canvas(dc.hwnd) if dc.hwnd else None
        if canvas is not None:
            canvas.paste(context.framebuffer.image().resize(canvas.size))

    def swap(self, dp, hdc):
        for context in self.contexts.values():
            if context.hdc == hdc:
                self.present(context)
        return 1

    # ------------------------------------------------------------------ state
    def call(self, name: str, *values):
        """A gl function of the current context."""
        context = self.ctx()
        if context is None:
            return 0
        method = getattr(self, "_" + name, None)
        if method is None:
            return 0
        result = method(context, *values)
        return 0 if result is None else result

    def _glClearColor(self, c, r, g, b, a):
        c.clear_color = [r, g, b, a]

    def _glClearDepth(self, c, d):
        c.clear_depth = d

    def _glClear(self, c, mask):
        fb = c.ensure()
        fb.clear(c.clear_color if mask & GL_COLOR_BUFFER_BIT else None, c.clear_depth if mask & GL_DEPTH_BUFFER_BIT else None)

    def _glViewport(self, c, x, y, w, h):
        c.viewport = [_i(x), _i(y), _i(w), _i(h)]

    def _glMatrixMode(self, c, mode):
        if mode in c.stacks:
            c.mode = mode

    def _glLoadIdentity(self, c):
        c.matrix = numpy.identity(4)

    def _glOrtho(self, c, l, r, b, t, n, f):
        c.matrix = c.matrix @ _ortho(l, r, b, t, n, f)

    def _glFrustum(self, c, l, r, b, t, n, f):
        c.matrix = c.matrix @ _frustum(l, r, b, t, n, f)

    def _glTranslatef(self, c, x, y, z):
        m = numpy.identity(4)
        m[:3, 3] = [x, y, z]
        c.matrix = c.matrix @ m

    _glTranslated = _glTranslatef

    def _glScalef(self, c, x, y, z):
        c.matrix = c.matrix @ numpy.diag([x, y, z, 1.0])

    _glScaled = _glScalef

    def _glRotatef(self, c, angle, x, y, z):
        c.matrix = c.matrix @ _rotate(angle, x, y, z)

    _glRotated = _glRotatef

    def _load(self, c, address, double, multiply):
        data = bytes(self.dp.read(address, 128 if double else 64))
        m = numpy.array(struct.unpack("<16d" if double else "<16f", data)).reshape(4, 4).T
        c.matrix = c.matrix @ m if multiply else m

    def _glLoadMatrixf(self, c, p):
        self._load(c, p, False, False)

    def _glLoadMatrixd(self, c, p):
        self._load(c, p, True, False)

    def _glMultMatrixf(self, c, p):
        self._load(c, p, False, True)

    def _glMultMatrixd(self, c, p):
        self._load(c, p, True, True)

    def _glPushMatrix(self, c):
        c.stacks[c.mode].append(c.matrix.copy())

    def _glPopMatrix(self, c):
        if len(c.stacks[c.mode]) > 1:
            c.stacks[c.mode].pop()
        else:
            c.error = 0x0504  # GL_STACK_UNDERFLOW

    def _glEnable(self, c, cap):
        c.enabled.add(cap)

    def _glDisable(self, c, cap):
        c.enabled.discard(cap)

    def _glIsEnabled(self, c, cap):
        return int(cap in c.enabled)

    def _glShadeModel(self, c, mode):
        c.flat = mode == GL_FLAT

    def _glFrontFace(self, c, mode):
        c.front = mode

    def _glCullFace(self, c, mode):
        c.cull = mode

    def _glPointSize(self, c, size):
        c.point_size = size

    def _glLineWidth(self, c, width):
        c.line_width = width

    def _glPixelStorei(self, c, name, value):
        if name == 0x0D05:  # GL_PACK_ALIGNMENT
            c.pack_alignment = value or 4
        elif name == 0x0CF5:  # GL_UNPACK_ALIGNMENT
            c.unpack_alignment = value or 4

    def _glGetError(self, c):
        error, c.error = c.error, 0
        return error

    def _glFlush(self, c):
        self.present(c)

    _glFinish = _glFlush

    # ------------------------------------------------------------------ vertices
    def _glBegin(self, c, mode):
        c.begin, c.vertices = mode, []

    def _vertex(self, c, x, y=0.0, z=0.0, w=1.0):
        if c.begin is not None:
            c.vertices.append(((x, y, z, w), tuple(c.color), tuple(c.texcoord)))

    def _color(self, c, r, g, b, a=1.0):
        c.color = [r, g, b, a]

    def _glEnd(self, c):
        if c.begin is None:
            return
        mode, vertices = c.begin, c.vertices
        c.begin, c.vertices = None, []
        self.draw(c, KINDS.get(mode, "points"), vertices)

    def draw(self, c: Context, kind: str, vertices: list):
        """Primitives of the fixed pipeline: vertices ((x, y, z, w), color, texture coordinates)."""
        if not vertices:
            return
        fb = c.ensure()
        positions = numpy.array([v[0] for v in vertices], dtype=numpy.float64)
        colors = numpy.array([v[1] for v in vertices], dtype=numpy.float64)
        uvs = numpy.array([v[2] if len(v) > 2 else (0.0, 0.0) for v in vertices], dtype=numpy.float64)
        transform = c.stacks[GL_PROJECTION][-1] @ c.stacks[GL_MODELVIEW][-1]
        clip = positions @ transform.T
        texture = c.textures.get(c.bound) if GL_TEXTURE_2D in c.enabled else None
        parameters = c.texture_parameters.get(c.bound, {})
        linear = parameters.get(0x2800, 0x2601) == 0x2601  # GL_TEXTURE_MAG_FILTER: GL_LINEAR
        address = tuple({0x812F: "clamp", 0x2900: "clamp", 0x8370: "mirror", 0x812D: "border"}.get(parameters.get(name, 0x2901), "wrap")
                        for name in (0x2802, 0x2803))
        mode = c.env_mode
        chain = getattr(c, "texture_levels", None) if texture is not None else None

        def fragment(frags):
            color = frags.attrs[:, :4]
            if texture is not None and chain is not None:
                from .raster import implicit_lod, sample_mips
                levels, mip_linear = chain
                uv = frags.attrs[:, 4:6]
                lod = implicit_lod(uv, (levels[0].shape[1], levels[0].shape[0]))
                texel = sample_mips(levels, uv[:, 0], uv[:, 1], lod, linear, mip_linear, address)
            elif texture is not None:
                texel = sample(texture, frags.attrs[:, 4], frags.attrs[:, 5], linear, address)
            if texture is not None:
                if mode == 0x1E01:      # GL_REPLACE
                    color = texel
                elif mode == 0x2101:    # GL_DECAL
                    color = numpy.concatenate([color[:, :3] * (1 - texel[:, 3:]) + texel[:, :3] * texel[:, 3:], color[:, 3:]], 1)
                elif mode == 0x0104:    # GL_ADD
                    color = numpy.concatenate([color[:, :3] + texel[:, :3], color[:, 3:] * texel[:, 3:]], 1)
                else:                   # GL_MODULATE
                    color = color * texel
            return [color], None, None

        fragment.quads = chain is not None
        flat = numpy.zeros(6, dtype=bool)
        flat[:4] = c.flat
        self.render(c, kind, clip, numpy.concatenate([colors, uvs], axis=1), fragment, flat)

    def render(self, c: Context, kind: str, clip, attrs, fragment, flat=None, targets=None):
        """Draws primitives of a primitive kind (KINDS) with the state of the context."""
        fb = c.ensure()
        prims = primitives(kind, len(clip))
        raster_kind = kind_of(kind)
        if raster_kind == "triangles" and GL_CULL_FACE in c.enabled and c.cull == GL_FRONT_AND_BACK:
            return
        cull = "none"
        if GL_CULL_FACE in c.enabled:
            cull = "back" if c.cull == GL_BACK else "front"
        x, y, w, h = c.viewport
        pipeline = Pipeline((x, y, w, h), "gl", c.depth_range, cull=cull, front_cw=c.front == GL_CCW, provoking="last",
                            point_size=c.point_size, line_width=c.line_width,
                            scissor=self.scissor_rect(c, fb.height))
        blend = Blend(GL_BLEND in c.enabled, *c.blend[1:], write_mask=c.color_mask)
        if targets is None:
            targets = [Target(fb.color, blend)]
            depth = fb.depth
        else:
            targets, depth = targets
        output = Output(targets, depth, c.depth_func if GL_DEPTH_TEST in c.enabled else 0, c.depth_mask, c.blend_color)
        draw(output, pipeline, raster_kind, prims, attrs, fragment, clip=clip, flat_mask=flat)

    def scissor_rect(self, c: Context, height: int):
        if GL_SCISSOR_TEST not in c.enabled or c.scissor is None:
            return None
        x, y, w, h = c.scissor
        return (x, height - (y + h), x + w, height - y)

    def _glBlendFunc(self, c, src, dst):
        c.blend = [c.blend[0], BLEND_FACTORS.get(src, 2), BLEND_FACTORS.get(dst, 1), c.blend[3],
                   BLEND_FACTORS.get(src, 2), BLEND_FACTORS.get(dst, 1), c.blend[6]]

    def _glDepthFunc(self, c, func):
        c.depth_func = func - 0x1FF if 0x200 <= func <= 0x207 else 2

    def _glDepthMask(self, c, flag):
        c.depth_mask = bool(flag)

    def _glColorMask(self, c, r, g, b, a):
        c.color_mask = (1 if r else 0) | (2 if g else 0) | (4 if b else 0) | (8 if a else 0)

    def _glDepthRange(self, c, near, far):
        c.depth_range = (max(0.0, min(1.0, near)), max(0.0, min(1.0, far)))

    def _glScissor(self, c, x, y, w, h):
        c.scissor = (_i(x), _i(y), _i(w), _i(h))

    # ------------------------------------------------------------------ textures
    def _glGenTextures(self, c, count, out):
        names = list(range(c.next_texture, c.next_texture + _i(count)))
        c.next_texture += len(names)
        self.dp.write(out, struct.pack(f"<{len(names)}I", *names))

    def _glDeleteTextures(self, c, count, names):
        for name in struct.unpack(f"<{_i(count)}I", bytes(self.dp.read(names, 4 * _i(count)))):
            c.textures.pop(name, None)

    def _glBindTexture(self, c, target, name):
        if target == GL_TEXTURE_2D:
            c.bound = name

    def _glIsTexture(self, c, name):
        return int(name in c.textures)

    def _glTexParameteri(self, c, target, name, value):
        c.texture_parameters.setdefault(c.bound, {})[name] = value

    def _glTexParameterf(self, c, target, name, value):
        self._glTexParameteri(c, target, name, int(value))

    def _glTexEnvi(self, c, target, name, value):
        if name == 0x2200:  # GL_TEXTURE_ENV_MODE
            c.env_mode = value

    def _glTexEnvf(self, c, target, name, value):
        self._glTexEnvi(c, target, name, int(value))

    def _pixels(self, c, width, height, fmt, kind, address):
        """Pixels of the program (glTexImage2D) as (h, w, 4) floats, the first row (t = 0) at the bottom."""
        channels = {0x1908: 4, 0x1907: 3, 0x80E1: 4, 0x80E0: 3, 0x1909: 1, 0x190A: 2, 0x1906: 1}.get(fmt)
        if channels is None or kind not in (0x1401, 0x1406) or not address or width <= 0 or height <= 0:
            return None
        size = channels * (4 if kind == 0x1406 else 1)
        row = (width * size + c.unpack_alignment - 1) // c.unpack_alignment * c.unpack_alignment
        data = numpy.frombuffer(bytes(self.dp.read(address, row * height)), dtype=numpy.uint8).reshape(height, row)
        data = numpy.ascontiguousarray(data[:, :width * size])
        values = (data.view(numpy.float32) if kind == 0x1406 else data / 255.0).reshape(height, width, channels)
        out = numpy.ones((height, width, 4), dtype=numpy.float32)
        if fmt in (0x80E1, 0x80E0):
            values = values[..., [2, 1, 0, 3][:channels]]
        if channels >= 3:
            out[..., :channels] = values
        elif fmt == 0x1906:          # GL_ALPHA
            out[..., 3] = values[..., 0]
        elif channels == 2:          # GL_LUMINANCE_ALPHA
            out[..., :3], out[..., 3] = values[..., :1], values[..., 1]
        else:                        # GL_LUMINANCE
            out[..., :3] = values[..., :1]
        return out

    def _glTexImage2D(self, c, target, level, internal, width, height, border, fmt, kind, data):
        if target != GL_TEXTURE_2D or level != 0:
            return
        width, height = _i(width), _i(height)
        pixels = self._pixels(c, width, height, fmt, kind, data)
        c.textures[c.bound] = pixels if pixels is not None else numpy.ones((max(1, height), max(1, width), 4), numpy.float32)

    def _glTexSubImage2D(self, c, target, level, x, y, width, height, fmt, kind, data):
        texture = c.textures.get(c.bound)
        if texture is None or level != 0:
            return
        x, y, width, height = _i(x), _i(y), _i(width), _i(height)
        pixels = self._pixels(c, width, height, fmt, kind, data)
        if pixels is not None:
            texture[y:y + height, x:x + width] = pixels[:texture.shape[0] - y, :texture.shape[1] - x]

    # ------------------------------------------------------------------ vertex arrays
    def _glEnableClientState(self, c, array):
        c.client.add(array)

    def _glDisableClientState(self, c, array):
        c.client.discard(array)

    def _glVertexPointer(self, c, size, kind, stride, pointer):
        c.arrays[0x8074] = (_i(size), kind, _i(stride), pointer)

    def _glColorPointer(self, c, size, kind, stride, pointer):
        c.arrays[0x8076] = (_i(size), kind, _i(stride), pointer)

    def _glTexCoordPointer(self, c, size, kind, stride, pointer):
        c.arrays[0x8078] = (_i(size), kind, _i(stride), pointer)

    def _array(self, c, array, index):
        size, kind, stride, pointer = c.arrays[array]
        fmt, scale = {0x1406: ("f", 1), 0x140A: ("d", 1), 0x1404: ("i", 1), 0x1402: ("h", 1), 0x1401: ("B", 255),
                      0x1400: ("b", 127), 0x1403: ("H", 65535), 0x1405: ("I", 4294967295)}.get(kind, ("f", 1))
        width = struct.calcsize(fmt) * size
        values = struct.unpack(f"<{size}{fmt}", bytes(self.dp.read(pointer + index * (stride or width), width)))
        return [v / scale for v in values] if array == 0x8076 and scale != 1 else [float(v) for v in values]

    def _array_vertices(self, c, indices):
        if 0x8074 not in c.client or 0x8074 not in c.arrays:
            return []
        out = []
        for index in indices:
            position = self._array(c, 0x8074, index)
            color = tuple((self._array(c, 0x8076, index) + [1.0])[:4]) if 0x8076 in c.client and 0x8076 in c.arrays \
                else tuple(c.color)
            uv = tuple(self._array(c, 0x8078, index)[:2]) if 0x8078 in c.client and 0x8078 in c.arrays else tuple(c.texcoord)
            xyz = tuple((position + [0.0, 0.0])[:3])
            out.append((xyz + ((position[3],) if len(position) > 3 else (1.0,)), color, uv))
        return out

    def _glDrawArrays(self, c, mode, first, count):
        self.draw(c, KINDS.get(mode, "points"), self._array_vertices(c, range(_i(first), _i(first) + _i(count))))

    def _glDrawElements(self, c, mode, count, kind, indices):
        fmt = {0x1401: "B", 0x1403: "H", 0x1405: "I"}.get(kind, "I")
        count = _i(count)
        values = struct.unpack(f"<{count}{fmt}", bytes(self.dp.read(indices, count * struct.calcsize(fmt))))
        self.draw(c, KINDS.get(mode, "points"), self._array_vertices(c, values))

    def _glArrayElement(self, c, index):
        if c.begin is not None:
            c.vertices.extend(self._array_vertices(c, [_i(index)]))

    def _glRectf(self, c, x1, y1, x2, y2):
        self.draw(c, "polygon", [((x1, y1, 0.0, 1.0), tuple(c.color)), ((x2, y1, 0.0, 1.0), tuple(c.color)),
                                 ((x2, y2, 0.0, 1.0), tuple(c.color)), ((x1, y2, 0.0, 1.0), tuple(c.color))])

    _glRectd = _glRectf

    def _glRecti(self, c, x1, y1, x2, y2):
        self._glRectf(c, _i(x1), _i(y1), _i(x2), _i(y2))

    # ------------------------------------------------------------------ queries
    def _glReadPixels(self, c, x, y, w, h, fmt, kind, out):
        fb = c.ensure()
        x, y, w, h = _i(x), _i(y), _i(w), _i(h)
        if kind not in (0x1401, 0x1406):  # GL_UNSIGNED_BYTE, GL_FLOAT
            c.error = 0x0500
            return
        channels = {0x1908: (0, 1, 2, 3), 0x1907: (0, 1, 2), 0x80E1: (2, 1, 0, 3), 0x80E0: (2, 1, 0), 0x1909: (0,)}.get(fmt)
        if channels is None:
            c.error = 0x0500
            return
        size = len(channels) * (4 if kind == 0x1406 else 1)
        row = (w * size + c.pack_alignment - 1) // c.pack_alignment * c.pack_alignment
        data = bytearray(row * h)
        for j in range(h):
            image_row = fb.height - 1 - (y + j)
            for i in range(w):
                px = x + i
                if 0 <= px < fb.width and 0 <= image_row < fb.height:
                    pixel = fb.color[image_row, px]
                    values = [int(pixel[k]) for k in channels]
                    chunk = struct.pack(f"<{len(values)}f", *[v / 255 for v in values]) if kind == 0x1406 else bytes(values)
                    data[j * row + i * size: j * row + (i + 1) * size] = chunk
        self.dp.write(out, bytes(data))

    def _glGetString(self, c, name):
        text = {0x1F00: "Fathom emulator", 0x1F01: "Fathom software rasterizer", 0x1F02: "1.1.0 Fathom emulator",
                0x1F03: ""}.get(name)
        if text is None:
            return 0
        if name not in self.strings:
            address = self.dp.allocate(len(text) + 1)
            self.dp.write(address, text.encode() + b"\0")
            self.strings[name] = address
        return self.strings[name]

    def _glGetIntegerv(self, c, name, out):
        values = {0x0BA2: c.viewport or [0, 0, 0, 0], 0x0BA0: [c.mode], 0x0D33: [4096], 0x0D3A: [4096, 4096]}.get(name, [0])
        self.dp.write(out, struct.pack(f"<{len(values)}i", *values))

    def _glGetFloatv(self, c, name, out):
        if name in (0x0BA6, 0x0BA7):  # GL_MODELVIEW_MATRIX, GL_PROJECTION_MATRIX
            m = c.stacks[GL_MODELVIEW if name == 0x0BA6 else GL_PROJECTION][-1]
            self.dp.write(out, struct.pack("<16f", *m.T.flatten()))
        elif name == 0x0C22:  # GL_COLOR_CLEAR_VALUE
            self.dp.write(out, struct.pack("<4f", *c.clear_color))
        else:
            self.dp.write(out, struct.pack("<f", 0.0))


def _sign(value: int, bits: int) -> int:
    value &= (1 << bits) - 1
    return value - (1 << bits) if value >> (bits - 1) else value


def _i(value) -> int:
    value = int(value) & 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def install(dp):
    if numpy is None:
        return None
    gl = OpenGl(dp)
    dp.opengl = gl
    hooks = dp.apihooks
    m = "opengl32.dll"
    # vertices and colors in their forms
    special = {}
    # the integers arrive unsigned (apihooks.read_args): their sign by their width
    signed = {"i": lambda x: _sign(x, 32), "s": lambda x: _sign(x, 16), "b": lambda x: _sign(x, 8), "ub": int, "us": int,
              "ui": int, "f": float, "d": float}
    for size in (2, 3, 4):
        for suffix in ("f", "d", "i", "s"):
            special[f"glVertex{size}{suffix}"] = (lambda conv: lambda dp, *v: gl.call(
                "vertex_values", [float(conv(x)) for x in v]))(signed[suffix])
            special[f"glTexCoord{size}{suffix}"] = (lambda conv: lambda dp, *v: gl.call(
                "texcoord_values", [float(conv(x)) for x in v]))(signed[suffix])
        for suffix, fmt in (("fv", "f"), ("dv", "d"), ("iv", "i"), ("sv", "h")):
            reader = (lambda n, f: lambda dp, p: [float(x) for x in struct.unpack(f"<{n}{f}", bytes(dp.read(p, n * struct.calcsize(f))))])(size, fmt)
            special[f"glVertex{size}{suffix}"] = (lambda r: lambda dp, p: gl.call("vertex_values", r(dp, p)))(reader)
            special[f"glTexCoord{size}{suffix}"] = (lambda r: lambda dp, p: gl.call("texcoord_values", r(dp, p)))(reader)
    for suffix in ("f", "d", "i", "s"):
        special[f"glTexCoord1{suffix}"] = (lambda conv: lambda dp, x: gl.call("texcoord_values", [float(conv(x))]))(signed[suffix])
    for size in (3, 4):
        for suffix, scale in (("f", 1.0), ("d", 1.0), ("ub", 255.0), ("b", 127.0), ("us", 65535.0), ("s", 32767.0),
                              ("ui", 4294967295.0), ("i", 2147483647.0)):
            special[f"glColor{size}{suffix}"] = (lambda s, conv: lambda dp, *v: gl.call(
                "color_values", [max(-1.0, conv(x) / s) for x in v]))(scale, signed[suffix])
        for suffix, fmt, scale in (("fv", "f", 1.0), ("dv", "d", 1.0), ("ubv", "B", 255.0), ("bv", "b", 127.0),
                                   ("usv", "H", 65535.0), ("sv", "h", 32767.0), ("uiv", "I", 4294967295.0), ("iv", "i", 2147483647.0)):
            special[f"glColor{size}{suffix}"] = (lambda n, f, s: lambda dp, p: gl.call("color_values", [x / s for x in struct.unpack(
                f"<{n}{f}", bytes(dp.read(p, n * struct.calcsize(f))))]))(size, fmt, scale)
    gl._vertex_values = lambda c, v: gl._vertex(c, *v)
    gl._texcoord_values = lambda c, v: setattr(c, "texcoord", (list(v) + [0.0, 0.0])[:2])
    gl._color_values = lambda c, v: gl._color(c, *v)
    for name, signature in OPENGL.items():
        handler = special.get(name)
        if handler is None:
            # looked up when called: gl_emu adds the methods of the exports beyond OpenGL 1.x (glGetTexImage...)
            handler = (lambda n: lambda dp, *v: gl.call(n, *v) if hasattr(gl, "_" + n) else _not_emulated(dp, n))(name)
        hooks.register_signature(m, name, signature, handler)
    for module in ("opengl32.dll",):
        hooks.register_signature(module, "wglCreateContext", "p", gl.create_context)
        hooks.register_signature(module, "wglDeleteContext", "p", gl.delete_context)
        hooks.register_signature(module, "wglMakeCurrent", "pp", gl.make_current)
        hooks.register_signature(module, "wglGetCurrentContext", "", lambda dp: gl.current.get(dp.thread_id, 0))
        hooks.register_signature(module, "wglGetCurrentDC", "", lambda dp: gl.contexts[gl.current[dp.thread_id]].hdc
                                 if dp.thread_id in gl.current else 0)
        hooks.register_signature(module, "wglShareLists", "pp", lambda dp, a, b: 1)
        hooks.register_signature(module, "wglSwapBuffers", "p", gl.swap)
        hooks.register_signature(module, "wglChoosePixelFormat", "pp", lambda dp, hdc, pfd: 1)
        hooks.register_signature(module, "wglSetPixelFormat", "ppp", lambda dp, hdc, f, pfd: 1)
        hooks.register_signature(module, "wglDescribePixelFormat", "pppp", gl.describe)
        hooks.register_signature(module, "wglGetPixelFormat", "p", lambda dp, hdc: 1)
    hooks.register_signature("gdi32.dll", "ChoosePixelFormat", "pp", lambda dp, hdc, pfd: 1)
    hooks.register_signature("gdi32.dll", "SetPixelFormat", "ppp", lambda dp, hdc, f, pfd: 1)
    hooks.register_signature("gdi32.dll", "DescribePixelFormat", "pppp", gl.describe)
    hooks.register_signature("gdi32.dll", "GetPixelFormat", "p", lambda dp, hdc: 1)
    hooks.register_signature("gdi32.dll", "SwapBuffers", "p", gl.swap)
    glu = "glu32.dll"
    hooks.register_signature(glu, "gluPerspective", "dddd", lambda dp, fovy, aspect, near, far: gl.call(
        "glFrustum_values", fovy, aspect, near, far))
    gl._glFrustum_values = lambda c, fovy, aspect, near, far: gl._glFrustum(
        c, -near * math.tan(math.radians(fovy) / 2) * aspect, near * math.tan(math.radians(fovy) / 2) * aspect,
        -near * math.tan(math.radians(fovy) / 2), near * math.tan(math.radians(fovy) / 2), near, far)
    hooks.register_signature(glu, "gluOrtho2D", "dddd", lambda dp, l, r, b, t: gl.call("glOrtho", l, r, b, t, -1.0, 1.0))
    hooks.register_signature(glu, "gluLookAt", "ddddddddd", lambda dp, *v: gl.call("look_at", *v))
    gl._look_at = _look_at
    from . import gl_emu  # OpenGL 1.2 to 4.x, GLSL; wglGetProcAddress
    gl_emu.install(gl)
    return gl


def _look_at(c, ex, ey, ez, cx, cy, cz, ux, uy, uz):
    forward = numpy.array([cx - ex, cy - ey, cz - ez])
    forward /= numpy.linalg.norm(forward) or 1.0
    up = numpy.array([ux, uy, uz])
    side = numpy.cross(forward, up)
    side /= numpy.linalg.norm(side) or 1.0
    up = numpy.cross(side, forward)
    m = numpy.identity(4)
    m[0, :3], m[1, :3], m[2, :3] = side, up, -forward
    t = numpy.identity(4)
    t[:3, 3] = [-ex, -ey, -ez]
    c.matrix = c.matrix @ m @ t


def _no_extension(dp, name: int) -> int:
    text = read_astring(dp, name) or ""
    missing = dp._fathom_unimplemented
    key = f"OpenGL extension {text}"
    missing[key] = missing.get(key, 0) + 1
    return 0


def _not_emulated(dp, name: str) -> int:
    missing = dp._fathom_unimplemented
    if name not in missing:
        dp.info(f"OpenGL {name} is not emulated (no effect)")
    missing[name] = missing.get(name, 0) + 1
    return 0
