# Fathom addition to dumpulator: the fixed function tessellator of Direct3D 11, a Python port of Microsoft's
# reference (CHWTessellator, "Microsoft D3D11 Fixed Function Tessellator Reference", as published in Mesa's
# src/gallium/auxiliary/tessellator/tessellator.cpp), so that the points AND the triangles of a patch are
# those a GPU makes: 16.16 fixed point positions, rings stitched with the reference's transition tables.
#
# The reference's license (MIT):
#
#     Copyright (c) Microsoft Corporation
#
#     Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
#     associated documentation files (the "Software"), to deal in the Software without restriction,
#     including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense,
#     and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so,
#     subject to the following conditions:
#
#     The above copyright notice and this permission notice shall be included in all copies or substantial
#     portions of the Software.
#
#     THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
#     NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
#     IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
#     WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
#     SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
# The port (this file) is under the same MIT terms.

import math
import struct

INTEGER, POW2, FRACTIONAL_ODD, FRACTIONAL_EVEN = 0, 1, 2, 3
OUTPUT_POINT, OUTPUT_LINE, OUTPUT_TRIANGLE_CW, OUTPUT_TRIANGLE_CCW = 0, 1, 2, 3
EVEN, ODD = 0, 1

MIN_ODD, MAX_ODD, MIN_EVEN, MAX_EVEN = 1, 63, 2, 64
MIN_ISOLINE_DENSITY, MAX_ISOLINE_DENSITY = 1, 64

FRACTION_BITS = 16
FRACTION_MASK = 0x0000ffff
INTEGER_MASK = 0x7fff0000
ONE = 1 << FRACTION_BITS
ONE_THIRD = 0x00005555
TWO_THIRDS = 0x0000aaaa
ONE_HALF = 0x00008000
EPSILON = 0.0000152587890625            # 2^-16
MIN_ODD_PLUS_HALF_EPSILON = MIN_ODD + EPSILON / 2
U32 = 0xFFFFFFFF

RECIPROCAL = [0xffffffff,
              0x10000, 0x8000, 0x5555, 0x4000, 0x3333, 0x2aab, 0x2492, 0x2000, 0x1c72, 0x199a, 0x1746, 0x1555,
              0x13b1, 0x1249, 0x1111, 0x1000, 0xf0f, 0xe39, 0xd79, 0xccd, 0xc31, 0xba3, 0xb21, 0xaab,
              0xa3d, 0x9d9, 0x97b, 0x925, 0x8d4, 0x889, 0x842, 0x800, 0x7c2, 0x788, 0x750, 0x71c,
              0x6eb, 0x6bd, 0x690, 0x666, 0x63e, 0x618, 0x5f4, 0x5d1, 0x5b0, 0x591, 0x572, 0x555,
              0x539, 0x51f, 0x505, 0x4ec, 0x4d5, 0x4be, 0x4a8, 0x492, 0x47e, 0x46a, 0x457, 0x444,
              0x432, 0x421, 0x410, 0x400]

FINAL_POINT_POSITION = [0, 32, 16, 8, 17, 4, 18, 9, 19, 2, 20, 10, 21, 5, 22, 11, 23,
                        1, 24, 12, 25, 6, 26, 13, 27, 3, 28, 14, 29, 7, 30, 15, 31]
LOOP_START = [1, 1, 17, 9, 9, 5, 5, 5, 5, 3, 3, 3, 3, 3, 3, 3, 3, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]
LOOP_END = [0, 0, 17, 17, 25, 25, 25, 25, 29, 29, 29, 29, 29, 29, 29, 29, 31, 31, 31, 31, 31, 31, 31, 31, 31, 31, 31, 31,
            31, 31, 31, 31, 32]

INSIDE_TO_OUTSIDE, INSIDE_TO_OUTSIDE_EXCEPT_MIDDLE, MIRRORED = 0, 1, 2


def _f32(x):
    return struct.unpack("<f", struct.pack("<f", x))[0]


def _bits(x):
    return struct.unpack("<i", struct.pack("<f", x))[0]


def _is_nan(a):
    return math.isnan(a)


def _flush(a):
    """Denormals flushed to zero (keeping the sign)."""
    b = _bits(a) & 0x7fffffff
    if b < 0x00800000:
        return -0.0 if _bits(a) & 0x80000000 else 0.0
    return a


def _fmin(a, b):
    fa, fb = _flush(a), _flush(b)
    if _is_nan(fb):
        return a
    if fa == 0 and fb == 0:
        return a if _bits(fa) & 0x80000000 else b
    return a if fa < fb else b


def _fmax(a, b):
    fa, fb = _flush(a), _flush(b)
    if _is_nan(fb):
        return a
    if fa == 0 and fb == 0:
        return a if _bits(fb) & 0x80000000 else b
    return a if fa >= fb else b


def _float_to_fixed(value):
    """floatToIDotF<15, 16, unsigned>: a float to 16.16 fixed point, rounded to nearest even."""
    i_bits, f_bits = 15, 16
    min_result, max_result = 0, ~0 & 0x7fffffff | 0x80000000
    max_result = U32                    # ~0 for the unsigned case, as an INT32 -1, then used as FXP
    mantissa_bits, exponent_bits = 23, 8
    bias = (1 << (exponent_bits - 1)) - 1
    hidden = 1 << mantissa_bits
    mantissa_mask = hidden - 1
    exponent_mask = ((1 << exponent_bits) - 1) << mantissa_bits
    sign_bit = 1 << (exponent_bits + mantissa_bits)
    max_pos = (bias + i_bits) << mantissa_bits
    shift = mantissa_bits + 1 - i_bits - f_bits
    if shift >= 0:
        max_pos -= 1 << shift
    max_neg = 0
    output = _bits(value) & U32
    unbiased = ((output & exponent_mask) >> mantissa_bits) - bias
    negative = output & sign_bit
    if unbiased == bias + 1 and output & mantissa_mask:
        return 0
    if not negative and output >= max_pos:
        return max_result
    if negative and (output & ~sign_bit & U32) >= max_neg:
        return min_result
    if unbiased < -f_bits - 1:
        return 0
    output = (output & mantissa_mask) | hidden
    extra = mantissa_bits - f_bits - unbiased
    if extra >= 0:
        lsb = 1 << extra
        half = lsb >> 1
        if (output & lsb) or (output & (lsb - 1)) > half:
            output += half
        output >>= extra
    else:
        output <<= -extra
    return output & U32


def _fixed_to_float(value):
    return float(value >> FRACTION_BITS) + float(value & FRACTION_MASK) / (1 << FRACTION_BITS)


def _is_even(value):
    return not (int(value) & 1)


def _ceil(value):
    return (value & INTEGER_MASK) + ONE if value & FRACTION_MASK else value


def _floor(value):
    return value & INTEGER_MASK


def _remove_msb(val):
    if val <= 0x0000ffff:
        check = 0x00000080 if val <= 0x000000ff else 0x00008000
    else:
        check = 0x00800000 if val <= 0x00ffffff else 0x80000000
    for _ in range(8):
        if val & check:
            return val & ~check
        check >>= 1
    return 0


class Context:
    """TESS_FACTOR_CONTEXT."""
    __slots__ = ("inv_floor", "inv_ceil", "half_fraction", "half_points", "split_point")


class Tessellator:
    """CHWTessellator: Init(partitioning, output primitive), then Tessellate{IsoLine,Tri,Quad}Domain; the
    points (u, v) and the indices."""

    def __init__(self, partitioning, output):
        self.original_partitioning = self.partitioning = partitioning
        self.parity = ODD if partitioning == FRACTIONAL_ODD else EVEN
        self.original_parity = self.parity
        self.output = output
        self.points = []
        self.indices = []
        self.patched = self.patched2 = False
        self.patch = {}
        self.patch2 = {}

    # -------------------------------------------------------------------------- helpers
    def odd(self):
        return self.parity == ODD

    def hw_integer(self):
        return self.partitioning in (INTEGER, POW2)

    def define_point(self, u, v, offset):
        while len(self.points) <= offset:
            self.points.append((0.0, 0.0))
        self.points[offset] = (_fixed_to_float(u & U32), _fixed_to_float(v & U32))

    def define_index(self, index, offset):
        index = self._patch_index(index)
        while len(self.indices) <= offset:
            self.indices.append(0)
        self.indices[offset] = index

    def define_cw_triangle(self, i0, i1, i2, base):
        self.define_index(i0, base)
        if self.output == OUTPUT_TRIANGLE_CW:
            self.define_index(i1, base + 1)
            self.define_index(i2, base + 2)
        else:
            self.define_index(i2, base + 1)
            self.define_index(i1, base + 2)

    def dump_all_points(self):
        for p in range(self.num_points):
            self.define_index(p, self.num_indices)
            self.num_indices += 1

    def dump_all_points_as_lines(self):
        for p in range(1, self.num_points):
            self.define_index(p - 1, self.num_indices)
            self.num_indices += 1
            self.define_index(p, self.num_indices)
            self.num_indices += 1

    def num_points_for(self, factor):
        if self.odd():
            return (_ceil(ONE_HALF + (factor + 1) // 2) * 2) >> FRACTION_BITS
        return ((_ceil((factor + 1) // 2) * 2) >> FRACTION_BITS) + 1

    def context(self, factor):
        ctx = Context()
        half = (factor + 1) // 2
        if self.odd() or half == ONE_HALF:
            half += ONE_HALF
        floor_half, ceil_half = _floor(half), _ceil(half)
        ctx.half_fraction = half - floor_half
        ctx.half_points = ceil_half >> FRACTION_BITS
        if ceil_half == floor_half:
            ctx.split_point = ctx.half_points + 1
        elif self.odd():
            ctx.split_point = 0 if floor_half == ONE else (_remove_msb((floor_half >> FRACTION_BITS) - 1) << 1) + 1
        else:
            ctx.split_point = (_remove_msb(floor_half >> FRACTION_BITS) << 1) + 1
        floor_segments = (floor_half * 2) >> FRACTION_BITS
        ceil_segments = (ceil_half * 2) >> FRACTION_BITS
        if self.odd():
            floor_segments -= 1
            ceil_segments -= 1
        ctx.inv_floor = RECIPROCAL[floor_segments]
        ctx.inv_ceil = RECIPROCAL[ceil_segments]
        return ctx

    def place(self, ctx, point):
        """PlacePointIn1D: the 16.16 position of a point on an edge."""
        flip = False
        if point >= ctx.half_points:
            point = (ctx.half_points << 1) - point
            if self.odd():
                point -= 1
            flip = True
        if point == ctx.half_points:
            return ONE_HALF
        on_ceil = point
        on_floor = on_ceil - 1 if point > ctx.split_point else on_ceil
        on_floor &= U32
        floor_location = (on_floor * ctx.inv_floor) & U32
        ceil_location = (on_ceil * ctx.inv_ceil) & U32
        location = (floor_location * ((ONE - ctx.half_fraction) & U32) + ceil_location * ctx.half_fraction) & U32
        location = ((location + ONE_HALF) & U32) >> FRACTION_BITS
        if flip:
            location = (ONE - location) & U32
        return location

    def _clamp(self, factors, lower, upper):
        return [_fmin(upper, _fmax(lower, f)) for f in factors]

    def _bounds(self):
        if self.original_partitioning in (INTEGER, POW2):
            return float(MIN_ODD), float(MAX_EVEN)
        if self.original_partitioning == FRACTIONAL_EVEN:
            return float(MIN_EVEN), float(MAX_EVEN)
        return float(MIN_ODD), float(MAX_ODD)

    def _patch_index(self, index):
        if self.patched:
            c = self.patch
            if index >= c["outside_base"]:
                return c["outside_replacement"] if index == c["outside_bad"] else index + c["outside_delta"]
            return c["inside_replacement"] if index == c["inside_bad"] else index + c["inside_delta"]
        if self.patched2:
            c = self.patch2
            if index >= c["invert_base"]:
                return c["corner_replacement"] if index == c["corner_bad"] else c["invert_end"] - index
            if index == c["corner_bad"]:
                return c["corner_replacement"]
        return index

    # -------------------------------------------------------------------------- stitching
    def stitch_regular(self, trapezoid, diagonals, base, inside_points, inside, outside):
        if trapezoid:
            self.define_cw_triangle(outside, outside + 1, inside, base)
            base += 3
            outside += 1
        if diagonals == INSIDE_TO_OUTSIDE:
            for _ in range(inside_points - 1):
                self.define_cw_triangle(inside, outside, outside + 1, base)
                base += 3
                self.define_cw_triangle(inside, outside + 1, inside + 1, base)
                base += 3
                inside += 1
                outside += 1
        elif diagonals == INSIDE_TO_OUTSIDE_EXCEPT_MIDDLE:
            p = 0
            while p < inside_points // 2 - 1:
                self.define_cw_triangle(outside, outside + 1, inside, base)
                base += 3
                self.define_cw_triangle(inside, outside + 1, inside + 1, base)
                base += 3
                inside += 1
                outside += 1
                p += 1
            self.define_cw_triangle(outside, inside + 1, inside, base)
            base += 3
            self.define_cw_triangle(outside, outside + 1, inside + 1, base)
            base += 3
            inside += 1
            outside += 1
            p += 2
            while p < inside_points:
                self.define_cw_triangle(outside, outside + 1, inside, base)
                base += 3
                self.define_cw_triangle(inside, outside + 1, inside + 1, base)
                base += 3
                inside += 1
                outside += 1
                p += 1
        else:   # MIRRORED
            p = 0
            while p < inside_points // 2:
                self.define_cw_triangle(outside, inside + 1, inside, base)
                base += 3
                self.define_cw_triangle(outside, outside + 1, inside + 1, base)
                base += 3
                inside += 1
                outside += 1
                p += 1
            while p < inside_points - 1:
                self.define_cw_triangle(inside, outside, outside + 1, base)
                base += 3
                self.define_cw_triangle(inside, outside + 1, inside + 1, base)
                base += 3
                inside += 1
                outside += 1
                p += 1
        if trapezoid:
            self.define_cw_triangle(outside, outside + 1, inside, base)

    def stitch_transition(self, base, inside, inside_half, inside_parity, outside, outside_half, outside_parity):
        if inside_parity == ODD:
            inside_half -= 1
        if outside_parity == ODD:
            outside_half -= 1
        start = min(LOOP_START[inside_half], LOOP_START[outside_half])
        end = max(LOOP_END[inside_half], LOOP_END[outside_half])
        if FINAL_POINT_POSITION[0] < outside_half:
            self.define_cw_triangle(outside, outside + 1, inside, base)
            base += 3
            outside += 1
        for i in range(start, end + 1):
            if FINAL_POINT_POSITION[i] < inside_half:
                self.define_cw_triangle(inside, outside, inside + 1, base)
                base += 3
                inside += 1
            if FINAL_POINT_POSITION[i] < outside_half:
                self.define_cw_triangle(outside, outside + 1, inside, base)
                base += 3
                outside += 1
        if inside_parity != outside_parity or inside_parity == ODD:
            if inside_parity == outside_parity:
                self.define_cw_triangle(inside, outside, inside + 1, base)
                base += 3
                self.define_cw_triangle(inside + 1, outside, outside + 1, base)
                base += 3
                inside += 1
                outside += 1
            elif inside_parity == EVEN:
                self.define_cw_triangle(inside, outside, outside + 1, base)
                base += 3
                outside += 1
            else:
                self.define_cw_triangle(inside, outside, inside + 1, base)
                base += 3
                inside += 1
        for i in range(end, start - 1, -1):
            if FINAL_POINT_POSITION[i] < outside_half:
                self.define_cw_triangle(outside, outside + 1, inside, base)
                base += 3
                outside += 1
            if FINAL_POINT_POSITION[i] < inside_half:
                self.define_cw_triangle(inside, outside, inside + 1, base)
                base += 3
                inside += 1
        if FINAL_POINT_POSITION[0] < outside_half:
            self.define_cw_triangle(outside, outside + 1, inside, base)

    # -------------------------------------------------------------------------- triangles
    def tessellate_tri(self, f_ueq0, f_veq0, f_weq0, f_inside):
        self.points, self.indices = [], []
        self.num_points = self.num_indices = 0
        p = self._tri_process(f_ueq0, f_veq0, f_weq0, f_inside)
        if p is None:
            return
        if p == "minimum":
            self.define_point(0, ONE, 0)
            self.define_point(0, 0, 1)
            self.define_point(ONE, 0, 2)
            self.num_points = 3
            if self.output in (OUTPUT_TRIANGLE_CW, OUTPUT_TRIANGLE_CCW):
                self.define_cw_triangle(0, 1, 2, self.num_indices)
                self.num_indices = 3
            elif self.output == OUTPUT_POINT:
                self.dump_all_points()
            else:
                self.dump_all_points_as_lines()
            return
        self._tri_points(p)
        if self.output == OUTPUT_POINT:
            self.dump_all_points()
        elif self.output == OUTPUT_LINE:
            self.dump_all_points_as_lines()
        else:
            self._tri_connectivity(p)

    def _tri_process(self, f0, f1, f2, inside):
        f0, f1, f2, inside = (_f32(x) for x in (f0, f1, f2, inside))
        if not (f0 > 0) or not (f1 > 0) or not (f2 > 0):
            return None
        lower, upper = self._bounds()
        outside = self._clamp([f0, f1, f2], lower, upper)
        if self.hw_integer():
            outside = [float(math.ceil(x)) for x in outside]
        if self.original_partitioning == FRACTIONAL_ODD and any(x > MIN_ODD_PLUS_HALF_EPSILON for x in outside):
            lower = _f32(MIN_ODD + EPSILON)
        inside = _fmin(upper, _fmax(lower, inside))
        if self.hw_integer():
            inside = float(math.ceil(inside))
        p = {}
        if self.hw_integer():
            p["outside_parity"] = [EVEN if _is_even(x) else ODD for x in outside]
            p["inside_parity"] = EVEN if (_is_even(inside) or inside == 1.0) else ODD
        else:
            p["outside_parity"] = [self.original_parity] * 3
            p["inside_parity"] = self.original_parity
        p["outside"] = [_float_to_fixed(x) for x in outside]
        p["inside"] = _float_to_fixed(inside)
        if self.hw_integer() or self.odd():
            if p["inside"] == ONE and all(x == ONE for x in p["outside"]):
                return "minimum"
        p["outside_ctx"] = []
        for e in range(3):
            self.parity = p["outside_parity"][e]
            p["outside_ctx"].append(self.context(p["outside"][e]))
        self.parity = p["inside_parity"]
        p["inside_ctx"] = self.context(p["inside"])
        p["outside_points"] = []
        for e in range(3):
            self.parity = p["outside_parity"][e]
            p["outside_points"].append(self.num_points_for(p["outside"][e]))
            self.num_points += p["outside_points"][e]
        self.num_points -= 3
        self.parity = p["inside_parity"]
        p["inside_points"] = max(4 if self.odd() else 3, self.num_points_for(p["inside"]))
        p["inside_base"] = self.num_points
        rings = (p["inside_points"] >> 1) - 1
        self.num_points += 3 * (rings * (rings + 1) - rings) if self.odd() else 3 * (rings * (rings + 1)) + 1
        return p

    def _tri_points(self, p):
        offset = 0
        for edge in range(3):
            parity = edge & 1
            end = p["outside_points"][edge] - 1
            for q0 in range(0, end):
                q = q0 if parity else end - q0
                self.parity = p["outside_parity"][edge]
                param = self.place(p["outside_ctx"][edge], q)
                if edge == 0:
                    self.define_point(0, param, offset)
                else:
                    self.define_point(param, (ONE - param) if edge == 2 else 0, offset)
                offset += 1
        self.parity = p["inside_parity"]
        rings = p["inside_points"] >> 1
        for ring in range(1, rings):
            start = ring
            end = p["inside_points"] - 1 - start
            for edge in range(3):
                parity = edge & 1
                perp = self.place(p["inside_ctx"], start)
                perp = (perp * TWO_THIRDS) & U32
                perp = ((perp + ONE_HALF) & U32) >> FRACTION_BITS
                for q0 in range(start, end):
                    q = q0 if parity else end - (q0 - start)
                    param = self.place(p["inside_ctx"], q)
                    shifted = (param - (perp + 1) // 2) & U32
                    if edge == 0:
                        self.define_point(perp, shifted, offset)
                    elif edge == 1:
                        self.define_point(shifted, perp, offset)
                    else:
                        self.define_point(shifted, (ONE - shifted - perp) & U32, offset)
                    offset += 1
        if not self.odd():
            self.define_point(ONE_THIRD, ONE_THIRD, offset)

    def _tri_connectivity(self, p):
        rings = (p["inside_points"] + 1) >> 1
        outside_ctx = list(p["outside_ctx"])
        outside_parity = list(p["outside_parity"])
        outside_points = list(p["outside_points"])
        inside_base = p["inside_base"]
        outside_base = 0
        for ring in range(1, rings):
            inside_points = p["inside_points"] - 2 * ring
            edge0_inside, edge0_outside = inside_base, outside_base
            for edge in range(3):
                triangles = inside_points + outside_points[edge] - 2
                if edge == 2:
                    c = self.patch = {}
                    c["inside_delta"] = inside_base
                    c["inside_bad"] = inside_points - 1
                    c["inside_replacement"] = edge0_inside
                    c["outside_base"] = c["inside_bad"] + 1
                    c["outside_delta"] = outside_base - c["outside_base"]
                    c["outside_bad"] = c["outside_base"] + outside_points[edge] - 1
                    c["outside_replacement"] = edge0_outside
                    self.patched = True
                    ib, ob = 0, c["outside_base"]
                else:
                    ib, ob = inside_base, outside_base
                if ring == 1:
                    self.stitch_transition(self.num_indices, ib, p["inside_ctx"].half_points, p["inside_parity"], ob,
                                           outside_ctx[edge].half_points, outside_parity[edge])
                else:
                    self.stitch_regular(True, MIRRORED, self.num_indices, inside_points, ib, ob)
                if edge == 2:
                    self.patched = False
                self.num_indices += triangles * 3
                outside_base += outside_points[edge] - 1
                inside_base += inside_points - 1
                outside_points[edge] = inside_points
            if ring == 1:
                outside_ctx = [p["inside_ctx"]] * 3
                outside_parity = [p["inside_parity"]] * 3
        self.parity = p["inside_parity"]
        if self.odd():
            self.define_cw_triangle(outside_base, outside_base + 1, outside_base + 2, self.num_indices)
            self.num_indices += 3

    # -------------------------------------------------------------------------- quads
    def tessellate_quad(self, f_ueq0, f_veq0, f_ueq1, f_veq1, inside_u, inside_v):
        self.points, self.indices = [], []
        self.num_points = self.num_indices = 0
        p = self._quad_process(f_ueq0, f_veq0, f_ueq1, f_veq1, inside_u, inside_v)
        if p is None:
            return
        if p == "minimum":
            self.define_point(0, 0, 0)
            self.define_point(ONE, 0, 1)
            self.define_point(ONE, ONE, 2)
            self.define_point(0, ONE, 3)
            self.num_points = 4
            if self.output in (OUTPUT_TRIANGLE_CW, OUTPUT_TRIANGLE_CCW):
                self.define_cw_triangle(0, 1, 3, 0)
                self.define_cw_triangle(1, 2, 3, 3)
                self.num_indices = 6
            elif self.output == OUTPUT_POINT:
                self.dump_all_points()
            else:
                self.dump_all_points_as_lines()
            return
        self._quad_points(p)
        if self.output == OUTPUT_POINT:
            self.dump_all_points()
        elif self.output == OUTPUT_LINE:
            self.dump_all_points_as_lines()
        else:
            self._quad_connectivity(p)

    def _quad_process(self, f0, f1, f2, f3, iu, iv):
        f0, f1, f2, f3, iu, iv = (_f32(x) for x in (f0, f1, f2, f3, iu, iv))
        if not (f0 > 0) or not (f1 > 0) or not (f2 > 0) or not (f3 > 0):
            return None
        lower, upper = self._bounds()
        outside = self._clamp([f0, f1, f2, f3], lower, upper)
        if self.hw_integer():
            outside = [float(math.ceil(x)) for x in outside]
        if self.original_partitioning == FRACTIONAL_ODD and \
                any(x > MIN_ODD_PLUS_HALF_EPSILON for x in outside + [iu, iv]):
            lower = _f32(MIN_ODD + EPSILON)
        inside = self._clamp([iu, iv], lower, upper)
        if self.hw_integer():
            inside = [float(math.ceil(x)) for x in inside]
        p = {}
        if self.hw_integer():
            p["outside_parity"] = [EVEN if _is_even(x) else ODD for x in outside]
            p["inside_parity"] = [EVEN if (_is_even(x) or x == 1.0) else ODD for x in inside]
        else:
            p["outside_parity"] = [self.original_parity] * 4
            p["inside_parity"] = [self.original_parity] * 2
        p["outside"] = [_float_to_fixed(x) for x in outside]
        p["inside"] = [_float_to_fixed(x) for x in inside]
        if self.hw_integer() or self.odd():
            if all(x == ONE for x in p["inside"]) and all(x == ONE for x in p["outside"]):
                return "minimum"
        p["outside_ctx"], p["inside_ctx"] = [], []
        for e in range(4):
            self.parity = p["outside_parity"][e]
            p["outside_ctx"].append(self.context(p["outside"][e]))
        for a in range(2):
            self.parity = p["inside_parity"][a]
            p["inside_ctx"].append(self.context(p["inside"][a]))
        p["outside_points"] = []
        for e in range(4):
            self.parity = p["outside_parity"][e]
            p["outside_points"].append(self.num_points_for(p["outside"][e]))
            self.num_points += p["outside_points"][e]
        self.num_points -= 4
        p["inside_points"] = []
        for a in range(2):
            self.parity = p["inside_parity"][a]
            count = self.num_points_for(p["inside"][a])
            p["inside_points"].append(max(4 if p["inside_parity"][a] == ODD else 3, count))
        p["inside_base"] = self.num_points
        self.num_points += (p["inside_points"][0] - 2) * (p["inside_points"][1] - 2)
        return p

    def _quad_points(self, p):
        offset = 0
        for edge in range(4):
            parity = edge & 1
            end = p["outside_points"][edge] - 1
            for q0 in range(0, end):
                q = q0 if edge in (1, 2) else end - q0
                self.parity = p["outside_parity"][edge]
                param = self.place(p["outside_ctx"][edge], q)
                if parity:
                    self.define_point(param, ONE if edge == 3 else 0, offset)
                else:
                    self.define_point(ONE if edge == 2 else 0, param, offset)
                offset += 1
        pu, pv = p["inside_points"]
        rings = min(pu, pv) >> 1
        for ring in range(1, rings):
            start = ring
            ends = [pu - 1 - start, pv - 1 - start]
            for edge in range(4):
                parity = [edge & 1, (edge + 1) & 1]
                perp_point = start if edge < 2 else ends[parity[0]]
                self.parity = p["inside_parity"][parity[0]]
                perp = self.place(p["inside_ctx"][parity[0]], perp_point)
                self.parity = p["inside_parity"][parity[1]]
                for q0 in range(start, ends[parity[1]]):
                    q = q0 if edge in (1, 2) else ends[parity[1]] - (q0 - start)
                    param = self.place(p["inside_ctx"][parity[1]], q)
                    if parity[1]:
                        self.define_point(perp, param, offset)
                    else:
                        self.define_point(param, perp, offset)
                    offset += 1
        if pu > pv and p["inside_parity"][1] == EVEN:
            start, end = rings, pu - 1 - rings
            self.parity = p["inside_parity"][0]
            for q in range(start, end + 1):
                self.define_point(self.place(p["inside_ctx"][0], q), ONE_HALF, offset)
                offset += 1
        elif pv >= pu and p["inside_parity"][0] == EVEN:
            start, end = rings, pv - 1 - rings
            self.parity = p["inside_parity"][1]
            for q in range(end, start - 1, -1):
                self.define_point(ONE_HALF, self.place(p["inside_ctx"][1], q), offset)
                offset += 1

    def _quad_connectivity(self, p):
        pu, pv = p["inside_points"]
        to_center = [(pu + 1) >> 1, (pv + 1) >> 1]
        rings = min(to_center)
        degenerate = [to_center[1] - 1 if p["inside_parity"][1] == EVEN else -1,
                      to_center[0] - 1 if p["inside_parity"][0] == EVEN else -1]
        outside_ctx = list(p["outside_ctx"])
        outside_parity = list(p["outside_parity"])
        outside_points = list(p["outside_points"])
        inside_base = p["inside_base"]
        outside_base = 0
        for ring in range(1, rings):
            inside_points = [pu - 2 * ring, pv - 2 * ring]
            edge0_inside, edge0_outside = inside_base, outside_base
            for edge in range(4):
                parity = (edge + 1) & 1
                triangles = inside_points[parity] + outside_points[edge] - 2
                if edge == 3:
                    if ring == degenerate[parity]:
                        c = self.patch2 = {}
                        c["invert_base"] = inside_base + 1
                        c["corner_bad"] = outside_base + outside_points[edge] - 1
                        c["corner_replacement"] = edge0_outside
                        c["invert_end"] = (c["invert_base"] << 1) - 1
                        ib, ob = c["invert_base"], outside_base
                        self.patched2 = True
                    else:
                        c = self.patch = {}
                        c["inside_delta"] = inside_base
                        c["inside_bad"] = inside_points[parity] - 1
                        c["inside_replacement"] = edge0_inside
                        c["outside_base"] = c["inside_bad"] + 1
                        c["outside_delta"] = outside_base - c["outside_base"]
                        c["outside_bad"] = c["outside_base"] + outside_points[edge] - 1
                        c["outside_replacement"] = edge0_outside
                        ib, ob = 0, c["outside_base"]
                        self.patched = True
                elif edge == 2 and ring == degenerate[parity]:
                    c = self.patch2 = {}
                    c["invert_base"] = inside_base
                    c["corner_bad"] = -1
                    c["corner_replacement"] = -1
                    c["invert_end"] = c["invert_base"] << 1
                    ib, ob = c["invert_base"], outside_base
                    self.patched2 = True
                else:
                    ib, ob = inside_base, outside_base
                if ring == 1:
                    self.stitch_transition(self.num_indices, ib, p["inside_ctx"][parity].half_points, p["inside_parity"][parity],
                                           ob, outside_ctx[edge].half_points, outside_parity[edge])
                else:
                    self.stitch_regular(True, MIRRORED, self.num_indices, inside_points[parity], ib, ob)
                self.patched = self.patched2 = False
                self.num_indices += triangles * 3
                outside_base += outside_points[edge] - 1
                if edge == 2 and ring == degenerate[parity]:
                    inside_base -= inside_points[parity] - 1
                else:
                    inside_base += inside_points[parity] - 1
                outside_points[edge] = inside_points[parity]
            if ring == 1:
                outside_ctx = [p["inside_ctx"][e & 1] for e in range(4)]
                outside_parity = [p["inside_parity"][e & 1] for e in range(4)]
        if pu > pv and p["inside_parity"][1] == ODD:
            self.patched2 = True
            quads = (((pu >> 1) - (pv >> 1)) << 1) + (2 if p["inside_parity"][0] == EVEN else 1)
            c = self.patch2 = {}
            c["invert_base"] = outside_base + quads + 2
            c["corner_bad"] = c["invert_base"]
            c["corner_replacement"] = outside_base
            c["invert_end"] = c["invert_base"] + c["invert_base"] + quads
            self.stitch_regular(False, INSIDE_TO_OUTSIDE, self.num_indices, quads + 1, c["invert_base"], outside_base + 1)
            self.patched2 = False
            self.num_indices += quads * 6
        elif pv >= pu and p["inside_parity"][0] == ODD:
            self.patched2 = True
            quads = (((pv >> 1) - (pu >> 1)) << 1) + (2 if p["inside_parity"][1] == EVEN else 1)
            c = self.patch2 = {}
            c["invert_base"] = outside_base + quads + 1
            c["corner_bad"] = -1
            c["corner_replacement"] = -1
            c["invert_end"] = c["invert_base"] + c["invert_base"] + quads
            diagonals = INSIDE_TO_OUTSIDE if p["inside_parity"][1] == EVEN else INSIDE_TO_OUTSIDE_EXCEPT_MIDDLE
            self.stitch_regular(False, diagonals, self.num_indices, quads + 1, c["invert_base"], outside_base)
            self.patched2 = False
            self.num_indices += quads * 6

    # -------------------------------------------------------------------------- isolines
    def tessellate_isoline(self, density, detail):
        self.points, self.indices = [], []
        self.num_points = self.num_indices = 0
        density, detail = _f32(density), _f32(detail)
        if not (density > 0) or not (detail > 0):
            return
        lower, upper = self._bounds()
        density = _fmin(float(MAX_ISOLINE_DENSITY), _fmax(float(MIN_ISOLINE_DENSITY), density))
        detail = _fmin(upper, _fmax(lower, detail))
        if self.hw_integer():
            detail = float(math.ceil(detail))
            detail_parity = EVEN if _is_even(detail) else ODD
        else:
            detail_parity = self.original_parity
        fx_detail = _float_to_fixed(detail)
        self.parity = detail_parity
        detail_ctx = self.context(fx_detail)
        per_line = self.num_points_for(fx_detail)
        self.partitioning = INTEGER
        density = float(math.ceil(density))
        density_parity = EVEN if _is_even(density) else ODD
        self.parity = density_parity
        fx_density = _float_to_fixed(density)
        density_ctx = self.context(fx_density)
        lines = self.num_points_for(fx_density) - 1
        self.partitioning = self.original_partitioning
        self.num_points = per_line * lines
        offset = 0
        for line in range(lines):
            for point in range(per_line):
                self.parity = density_parity
                v = self.place(density_ctx, line)
                self.parity = detail_parity
                u = self.place(detail_ctx, point)
                self.define_point(u, v, offset)
                offset += 1
        index = 0
        offset = 0
        for line in range(lines):
            for point in range(per_line):
                if self.output == OUTPUT_POINT:
                    self.define_index(offset, index)
                    index += 1
                elif point > 0:
                    self.define_index(offset - 1, index)
                    self.define_index(offset, index + 1)
                    index += 2
                offset += 1
        self.num_indices = index
