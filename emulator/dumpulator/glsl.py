# Fathom addition to dumpulator: a GLSL compiler / interpreter for the OpenGL of opengl_emu.py: vertex and
# fragment shaders, GLSL 1.10 to 4.60 (core and compatibility). Boost Software License 1.0, like the rest
# of dumpulator (LICENSE.dumpulator).
#
# The source goes through a preprocessor (#define with and without parameters, #if / #ifdef / #elif /
# #else / #endif with defined(), #version, #extension), a parser (declarations with their qualifiers and
# layouts, structs, arrays, uniform blocks, functions, every statement and operator) and runs on all the
# lanes at once (the vertices of a draw, the fragments of a batch): a value is a numpy array (n,) for a
# scalar, (n, k) for a vector, (n, columns, rows) for a matrix; the control flow (if, loops, break,
# continue, return, discard, switch) masks of active lanes. The built-in functions of GLSL and the
# built-in variables of both profiles (gl_Position, gl_FragColor, gl_FragData, gl_Vertex, gl_Color,
# gl_MultiTexCoord*, gl_TexCoord, gl_ModelViewProjectionMatrix, ftransform...) are implemented;
# derivatives (dFdx, dFdy, fwidth, coarse and fine) and the mip level of texture() come from the 2x2 quads
# of the fragment stage (raster.derivative). The geometry (EmitVertex / EndPrimitive, streams, invocations),
# tessellation control and evaluation, and compute shaders run too (run_geometry, run_stage, run_compute),
# with the variables in memory: buffer blocks (std430 / std140), shared variables (one copy per work
# group; all the lanes run in lockstep, so a barrier holds), atomic counters, the atomic functions (lane
# after lane, in order), and images (imageLoad / imageStore / imageSize / imageAtomic*). Not covered:
# subroutines.

import math
import re
from typing import Dict, List, Optional

import numpy


class GLSLError(Exception):
    pass


# ------------------------------------------------------------------ types
SCALARS = {"float": "float", "double": "float", "int": "int", "uint": "uint", "bool": "bool"}
VECTORS = {}
for _prefix, _base in (("vec", "float"), ("dvec", "float"), ("ivec", "int"), ("uvec", "uint"), ("bvec", "bool")):
    for _n in (2, 3, 4):
        VECTORS[f"{_prefix}{_n}"] = (_base, _n)
MATRICES = {}
for _c in (2, 3, 4):
    for _r in (2, 3, 4):
        MATRICES[f"mat{_c}x{_r}"] = (_c, _r)
        MATRICES[f"dmat{_c}x{_r}"] = (_c, _r)
    MATRICES[f"mat{_c}"] = (_c, _c)
    MATRICES[f"dmat{_c}"] = (_c, _c)
SAMPLERS = {"sampler1D", "sampler2D", "sampler3D", "samplerCube", "sampler2DRect", "sampler1DArray", "sampler2DArray",
            "samplerCubeArray", "sampler1DShadow", "sampler2DShadow", "samplerCubeShadow", "sampler2DArrayShadow",
            "sampler2DRectShadow", "samplerBuffer", "sampler2DMS", "isampler2D", "usampler2D", "isampler3D", "usampler3D",
            "isamplerCube", "usamplerCube", "isampler2DArray", "usampler2DArray", "isamplerBuffer", "usamplerBuffer",
            "sampler2DMSArray"}
QUALIFIERS = {"in", "out", "inout", "uniform", "attribute", "varying", "const", "flat", "smooth", "noperspective",
              "centroid", "sample", "invariant", "highp", "mediump", "lowp", "precise", "buffer", "shared", "coherent",
              "volatile", "restrict", "readonly", "writeonly", "patch"}


def base_of(kind: str) -> str:
    if kind in SCALARS:
        return SCALARS[kind]
    if kind in VECTORS:
        return VECTORS[kind][0]
    return "float"


def size_of(kind: str) -> int:
    if kind in VECTORS:
        return VECTORS[kind][1]
    if kind in MATRICES:
        c, r = MATRICES[kind]
        return c * r
    return 1


def vector_kind(base: str, n: int) -> str:
    if n == 1:
        return base
    return {"float": "vec", "int": "ivec", "uint": "uvec", "bool": "bvec"}[base] + str(n)


def dtype_of(base: str):
    return {"float": numpy.float64, "int": numpy.int64, "uint": numpy.int64, "bool": bool}[base]


class Val:
    """A value: kind (a GLSL type, a struct name, "array"), data (numpy array, list for arrays, dict for
    structs, int for a sampler)."""

    __slots__ = ("kind", "data", "length")

    def __init__(self, kind, data, length=0):
        self.kind, self.data, self.length = kind, data, length

    def copy(self):
        if self.kind == "array":
            return Val("array", [v.copy() for v in self.data], self.length)
        if isinstance(self.data, dict):
            return Val(self.kind, {k: v.copy() for k, v in self.data.items()})
        if isinstance(self.data, numpy.ndarray):
            return Val(self.kind, self.data.copy())
        return Val(self.kind, self.data)

    def __repr__(self):
        return f"Val({self.kind}, {self.data if not isinstance(self.data, numpy.ndarray) else self.data.shape})"


def zero(kind: str, n: int, structs=None, array=None) -> Val:
    if array is not None:
        return Val("array", [zero(kind, n, structs) for _ in range(array)], array)
    if structs and kind in structs:
        return Val(kind, {name: zero(field_kind, n, structs, field_array) for name, field_kind, field_array in structs[kind]})
    if kind in SAMPLERS:
        return Val(kind, 0)
    if kind in MATRICES:
        c, r = MATRICES[kind]
        return Val(kind, numpy.zeros((n, c, r)))
    if kind in VECTORS:
        base, k = VECTORS[kind]
        return Val(kind, numpy.zeros((n, k), dtype=dtype_of(base)))
    return Val(kind, numpy.zeros(n, dtype=dtype_of(base_of(kind))))


def as_columns(v: Val, n: int) -> numpy.ndarray:
    """A scalar / vector / matrix value as (n, components) (matrices column after column)."""
    data = v.data
    if v.kind in MATRICES:
        return data.reshape(n, -1)
    if data.ndim == 1:
        return data[:, None]
    return data


def convert(v: Val, base: str) -> numpy.ndarray:
    data = v.data
    if base == "float":
        return data.astype(numpy.float64)
    if base in ("int", "uint"):
        if data.dtype == bool:
            return data.astype(numpy.int64)
        if data.dtype.kind == "f":
            values = numpy.trunc(numpy.nan_to_num(data)).astype(numpy.int64)
            return values & 0xFFFFFFFF if base == "uint" else values
        return data.astype(numpy.int64)
    return data != 0


# ------------------------------------------------------------------ the preprocessor
def preprocess(source: str, defines: Optional[Dict[str, str]] = None):
    """The source without its directives (macros expanded), and its #version."""
    macros: Dict[str, tuple] = {"GL_core_profile": (None, "1"), "GL_compatibility_profile": (None, "1"),
                                "GL_FRAGMENT_PRECISION_HIGH": (None, "1")}
    for name, value in (defines or {}).items():
        macros[name] = (None, value)
    version = 110
    out = []
    stack = []  # (active, taken)
    source = source.replace("\\\r\n", "").replace("\\\n", "")
    for line in source.splitlines():
        stripped = line.strip()
        active = all(a for a, _ in stack)
        if stripped.startswith("#"):
            directive = stripped[1:].strip()
            word = directive.split(None, 1)[0] if directive else ""
            rest = directive[len(word):].strip()
            if word in ("ifdef", "ifndef"):
                defined = rest.split()[0] in macros if rest else False
                value = defined if word == "ifdef" else not defined
                stack.append((value and active, value))
            elif word == "if":
                value = bool(_eval_condition(rest, macros)) if active else False
                stack.append((value and active, value))
            elif word == "elif":
                parent = all(a for a, _ in stack[:-1])
                if stack:
                    _, taken = stack.pop()
                    value = (not taken) and bool(_eval_condition(rest, macros)) and parent
                    stack.append((value, taken or value))
            elif word == "else":
                parent = all(a for a, _ in stack[:-1])
                if stack:
                    _, taken = stack.pop()
                    stack.append(((not taken) and parent, True))
            elif word == "endif":
                if stack:
                    stack.pop()
            elif not active:
                pass
            elif word == "define":
                match = re.match(r"(\w+)(\(([^)]*)\))?\s*(.*)", rest)
                if match:
                    name, _, params, body = match.groups()
                    macros[name] = ([p.strip() for p in params.split(",") if p.strip()] if params is not None and
                                    rest[len(name)] == "(" else None, body)
            elif word == "undef":
                macros.pop(rest.split()[0] if rest else "", None)
            elif word == "version":
                version = int(rest.split()[0]) if rest else 110
            out.append("")
            continue
        out.append(_expand(line, macros) if active else "")
    return "\n".join(out), version


def _expand(line: str, macros, depth=0) -> str:
    if depth > 16:
        return line

    def replace(match):
        name = match.group(0)
        if name not in macros:
            return name
        params, body = macros[name]
        if params is None:
            return _expand(body, macros, depth + 1)
        return name
    result = re.sub(r"\b[A-Za-z_]\w*\b", replace, line)
    # function-like macros
    for name, (params, body) in macros.items():
        if params is None:
            continue
        while True:
            match = re.search(r"\b%s\s*\(" % re.escape(name), result)
            if not match:
                break
            start, i, level = match.start(), match.end(), 1
            args, current = [], ""
            while i < len(result) and level:
                char = result[i]
                if char == "(":
                    level += 1
                elif char == ")":
                    level -= 1
                    if level == 0:
                        break
                if char == "," and level == 1:
                    args.append(current)
                    current = ""
                else:
                    current += char
                i += 1
            args.append(current)
            text = body
            for param, arg in zip(params, args):
                text = re.sub(r"\b%s\b" % re.escape(param), arg.strip(), text)
            result = result[:start] + "(" + _expand(text, macros, depth + 1) + ")" + result[i + 1:]
    return result


def _eval_condition(text: str, macros) -> int:
    text = re.sub(r"defined\s*\(\s*(\w+)\s*\)", lambda m: "1" if m.group(1) in macros else "0", text)
    text = re.sub(r"defined\s+(\w+)", lambda m: "1" if m.group(1) in macros else "0", text)
    text = _expand(text, macros)
    text = re.sub(r"\b[A-Za-z_]\w*\b", "0", text)
    text = text.replace("&&", " and ").replace("||", " or ").replace("!", " not ").replace(" not =", "!=")
    try:
        return int(eval(text, {"__builtins__": {}}, {}))
    except Exception:
        return 0


# ------------------------------------------------------------------ the parser
TOKEN = re.compile(r"""\s*(?:
    (?P<float>(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?[fF]?|\d+[eE][+-]?\d+[fF]?|\d+[fF]) |
    (?P<int>0[xX][0-9a-fA-F]+[uU]?|\d+[uU]?) |
    (?P<name>[A-Za-z_]\w*) |
    (?P<op><<=|>>=|\+\+|--|<<|>>|<=|>=|==|!=|&&|\|\||\^\^|\+=|-=|\*=|/=|%=|&=|\|=|\^=|[-+*/%<>=!~&|^?:;,.(){}\[\]])
)""", re.X)


def tokenize(source: str) -> List[tuple]:
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    source = re.sub(r"//[^\n]*", "", source)
    tokens = []
    position = 0
    while position < len(source):
        match = TOKEN.match(source, position)
        if not match or match.end() == position:
            if source[position:].strip() == "":
                break
            raise GLSLError(f"unexpected character {source[position:position + 10]!r}")
        position = match.end()
        kind = match.lastgroup
        text = match.group(kind)
        tokens.append((kind, text))
    tokens.append(("end", ""))
    return tokens


class Node:
    __slots__ = ("op", "args", "value")

    def __init__(self, op, *args, value=None):
        self.op, self.args, self.value = op, list(args), value

    def __repr__(self):
        return f"Node({self.op}, {self.value!r}, {self.args})"


BINARY = [("||",), ("^^",), ("&&",), ("|",), ("^",), ("&",), ("==", "!="), ("<", ">", "<=", ">="), ("<<", ">>"),
          ("+", "-"), ("*", "/", "%")]
ASSIGNMENTS = {"=", "+=", "-=", "*=", "/=", "%=", "<<=", ">>=", "&=", "|=", "^="}


class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.i = 0
        self.structs: Dict[str, list] = {}

    def peek(self, offset=0):
        return self.tokens[min(self.i + offset, len(self.tokens) - 1)]

    def next(self):
        token = self.tokens[self.i]
        self.i += 1
        return token

    def accept(self, text):
        if self.peek()[1] == text and self.peek()[0] != "end":
            self.i += 1
            return True
        return False

    def expect(self, text):
        token = self.next()
        if token[1] != text:
            raise GLSLError(f"expected {text!r}, found {token[1]!r}")
        return token

    def is_type(self, text):
        return text in SCALARS or text in VECTORS or text in MATRICES or text in SAMPLERS or text in self.structs or \
            text == "void"

    # declarations -----------------------------------------------------------
    def unit(self):
        items = []
        while self.peek()[0] != "end":
            if self.accept(";"):
                continue
            items.extend(self.external())
        return items

    def qualifiers(self):
        quals, layout = [], {}
        while True:
            text = self.peek()[1]
            if text == "layout":
                self.next()
                self.expect("(")
                while not self.accept(")"):
                    name = self.next()[1]
                    if self.accept("="):
                        value = self.expression_noassign()
                        layout[name] = value
                    else:
                        layout[name] = None
                    self.accept(",")
            elif text in QUALIFIERS:
                quals.append(self.next()[1])
            else:
                return quals, layout

    def type_name(self):
        token = self.next()
        if token[1] == "struct":
            return self.struct_definition()
        return token[1]

    def struct_definition(self):
        name = self.next()[1] if self.peek()[0] == "name" and self.peek()[1] != "{" else f"__struct{len(self.structs)}"
        self.expect("{")
        fields = []
        while not self.accept("}"):
            self.qualifiers()
            kind = self.type_name()
            array = self.array_suffix()
            while True:
                field = self.next()[1]
                field_array = self.array_suffix() or array
                fields.append((field, kind, field_array))
                if not self.accept(","):
                    break
            self.expect(";")
        self.structs[name] = fields
        return name

    def array_suffix(self):
        if self.peek()[1] != "[":
            return None
        self.next()
        if self.accept("]"):
            return -1
        size = self.expression()
        self.expect("]")
        return size

    def external(self):
        if self.peek()[1] == "precision":
            while not self.accept(";"):
                self.next()
            return []
        quals, layout = self.qualifiers()
        if self.accept(";"):
            # layout(local_size_x = 8) in; layout(triangles) in; layout(vertices = 3) out; ...
            return [Node("layout_decl", value=dict(qualifiers=quals, layout=layout))] if layout else []
        # an interface block: qualifier Name { ... } instance;
        if self.peek()[0] == "name" and self.peek(1)[1] == "{" and not self.is_type(self.peek()[1]):
            block = self.next()[1]
            self.expect("{")
            members = []
            while not self.accept("}"):
                member_quals, member_layout = self.qualifiers()
                kind = self.type_name()
                array = self.array_suffix()
                while True:
                    name = self.next()[1]
                    members.append((name, kind, self.array_suffix() or array, member_layout))
                    if not self.accept(","):
                        break
                self.expect(";")
            instance = None
            instance_array = None
            if self.peek()[0] == "name":
                instance = self.next()[1]
                instance_array = self.array_suffix()
            self.expect(";")
            return [Node("block", value=dict(name=block, qualifiers=quals, layout=layout, members=members,
                                             instance=instance, array=instance_array))]
        kind = self.type_name()
        if self.accept(";"):
            return []
        array = self.array_suffix()
        name = self.next()[1]
        if self.peek()[1] == "(":
            return [self.function(kind, name)]
        return [self.declaration_rest(quals, layout, kind, array, name, top=True)]

    def declaration_rest(self, quals, layout, kind, array, name, top=False):
        items = []
        while True:
            own_array = self.array_suffix() or array
            init = None
            if self.accept("="):
                init = self.initializer()
            items.append((name, own_array, init))
            if not self.accept(","):
                break
            name = self.next()[1]
        self.expect(";")
        return Node("declare", value=dict(qualifiers=quals, layout=layout, kind=kind, items=items, global_=top))

    def initializer(self):
        if self.peek()[1] == "{":
            self.next()
            values = []
            while not self.accept("}"):
                values.append(self.initializer())
                self.accept(",")
            return Node("list", *values)
        return self.expression_noassign()

    def function(self, kind, name):
        self.expect("(")
        params = []
        if not self.accept(")"):
            while True:
                quals, _ = self.qualifiers()
                if self.peek()[1] == "void" and self.peek(1)[1] == ")":
                    self.next()
                    self.expect(")")
                    break
                param_kind = self.type_name()
                param_array = self.array_suffix()
                param_name = self.next()[1] if self.peek()[0] == "name" else f"__unnamed{len(params)}"
                param_array = self.array_suffix() or param_array
                params.append((param_name, param_kind, param_array, quals))
                if self.accept(")"):
                    break
                self.expect(",")
        if self.accept(";"):
            return Node("prototype", value=dict(name=name))
        body = self.block()
        return Node("function", body, value=dict(name=name, kind=kind, params=params))

    # statements -------------------------------------------------------------
    def block(self):
        self.expect("{")
        statements = []
        while not self.accept("}"):
            statements.append(self.statement())
        return Node("block_stmt", *statements)

    def statement(self):
        kind, text = self.peek()
        if text == "{":
            return self.block()
        if text == ";":
            self.next()
            return Node("nop")
        if text == "if":
            self.next()
            self.expect("(")
            condition = self.expression()
            self.expect(")")
            then = self.statement()
            other = self.statement() if self.accept("else") else None
            return Node("if", condition, then, other)
        if text == "for":
            self.next()
            self.expect("(")
            init = self.statement()
            condition = None if self.peek()[1] == ";" else self.condition_expression()
            self.expect(";")
            step = None if self.peek()[1] == ")" else self.expression()
            self.expect(")")
            return Node("for", init, condition, step, self.statement())
        if text == "while":
            self.next()
            self.expect("(")
            condition = self.condition_expression()
            self.expect(")")
            return Node("for", Node("nop"), condition, None, self.statement())
        if text == "do":
            self.next()
            body = self.statement()
            self.expect("while")
            self.expect("(")
            condition = self.expression()
            self.expect(")")
            self.expect(";")
            return Node("do", body, condition)
        if text == "switch":
            self.next()
            self.expect("(")
            value = self.expression()
            self.expect(")")
            self.expect("{")
            cases = []
            while not self.accept("}"):
                if self.accept("case"):
                    label = self.expression()
                    self.expect(":")
                    cases.append(["case", label, []])
                elif self.accept("default"):
                    self.expect(":")
                    cases.append(["default", None, []])
                else:
                    if not cases:
                        cases.append(["default", None, []])
                    cases[-1][2].append(self.statement())
            return Node("switch", value, value=cases)
        if text in ("break", "continue", "discard"):
            self.next()
            self.expect(";")
            return Node(text)
        if text == "return":
            self.next()
            value = None if self.peek()[1] == ";" else self.expression()
            self.expect(";")
            return Node("return", value)
        # a declaration?
        save = self.i
        quals, layout = self.qualifiers()
        if self.is_type(self.peek()[1]) or self.peek()[1] == "struct":
            if self.peek()[1] == "struct" or (self.peek(1)[0] == "name" or self.peek(1)[1] == "["):
                kind = self.type_name()
                if self.accept(";"):
                    return Node("nop")
                array = self.array_suffix()
                name = self.next()[1]
                return self.declaration_rest(quals, layout, kind, array, name)
        self.i = save
        expression = self.expression()
        self.expect(";")
        return Node("expr", expression)

    def condition_expression(self):
        save = self.i
        if self.is_type(self.peek()[1]) and self.peek(1)[0] == "name" and self.peek(2)[1] == "=":
            kind = self.next()[1]
            name = self.next()[1]
            self.expect("=")
            init = self.expression()
            return Node("cond_declare", init, value=(kind, name))
        self.i = save
        return self.expression()

    # expressions ------------------------------------------------------------
    def expression(self):
        node = self.assignment()
        while self.accept(","):
            node = Node("comma", node, self.assignment())
        return node

    def expression_noassign(self):
        return self.assignment()

    def assignment(self):
        left = self.ternary()
        if self.peek()[1] in ASSIGNMENTS and self.peek()[0] == "op":
            op = self.next()[1]
            right = self.assignment()
            return Node("assign", left, right, value=op)
        return left

    def ternary(self):
        condition = self.binary(0)
        if self.accept("?"):
            a = self.assignment()
            self.expect(":")
            b = self.assignment()
            return Node("ternary", condition, a, b)
        return condition

    def binary(self, level):
        if level == len(BINARY):
            return self.unary()
        left = self.binary(level + 1)
        while self.peek()[0] == "op" and self.peek()[1] in BINARY[level]:
            op = self.next()[1]
            right = self.binary(level + 1)
            left = Node("binary", left, right, value=op)
        return left

    def unary(self):
        token = self.peek()
        if token[0] == "op" and token[1] in ("+", "-", "!", "~"):
            self.next()
            return Node("unary", self.unary(), value=token[1])
        if token[0] == "op" and token[1] in ("++", "--"):
            self.next()
            return Node("preinc", self.unary(), value=token[1])
        return self.postfix(self.primary())

    def postfix(self, node):
        while True:
            token = self.peek()
            if token[1] == "[":
                self.next()
                index = self.expression()
                self.expect("]")
                node = Node("index", node, index)
            elif token[1] == ".":
                self.next()
                field = self.next()[1]
                if self.peek()[1] == "(" and field == "length":
                    self.next()
                    self.expect(")")
                    node = Node("length", node)
                else:
                    node = Node("field", node, value=field)
            elif token[1] in ("++", "--") and token[0] == "op":
                self.next()
                node = Node("postinc", node, value=token[1])
            else:
                return node

    def primary(self):
        kind, text = self.next()
        if kind == "float":
            return Node("const", value=("float", float(text.rstrip("fF"))))
        if kind == "int":
            unsigned = text[-1] in "uU"
            value = int(text.rstrip("uU"), 0) if not text.startswith("0") or text.lower().startswith("0x") or text == "0" \
                else int(text.rstrip("uU"), 8)
            return Node("const", value=("uint" if unsigned else "int", value))
        if text in ("true", "false"):
            return Node("const", value=("bool", text == "true"))
        if text == "(":
            node = self.expression()
            self.expect(")")
            return node
        if kind == "name":
            if self.peek()[1] == "[" and self.is_type(text) and self.peek(1)[1] in ("]",) or \
                    (self.is_type(text) and self.peek()[1] == "[" and self._array_constructor()):
                self.expect("[")
                size = None if self.peek()[1] == "]" else self.expression()
                self.expect("]")
                args = self.arguments()
                return Node("array_constructor", *args, value=(text, size))
            if self.peek()[1] == "(":
                args = self.arguments()
                return Node("call", *args, value=text)
            return Node("name", value=text)
        raise GLSLError(f"unexpected {text!r}")

    def _array_constructor(self):
        depth, j = 0, self.i
        while j < len(self.tokens):
            text = self.tokens[j][1]
            if text == "[":
                depth += 1
            elif text == "]":
                depth -= 1
                if depth == 0:
                    return self.tokens[j + 1][1] == "("
            j += 1
        return False

    def arguments(self):
        self.expect("(")
        args = []
        if self.accept(")"):
            return args
        while True:
            if self.peek()[1] == "void" and self.peek(1)[1] == ")":
                self.next()
                self.expect(")")
                return args
            args.append(self.assignment())
            if self.accept(")"):
                return args
            self.expect(",")


class Program:
    """A parsed shader: globals (declarations, uniform blocks), functions, structs."""

    def __init__(self, source: str, stage: str, defines=None):
        text, self.version = preprocess(source, defines)
        parser = Parser(tokenize(text))
        self.items = parser.unit()
        self.structs = parser.structs
        self.stage = stage
        self.functions: Dict[str, list] = {}
        self.globals: List[Node] = []
        self.blocks: List[dict] = []
        self.layout_decls: List[dict] = []
        for item in self.items:
            if item.op == "layout_decl":
                self.layout_decls.append(item.value)
            elif item.op == "function":
                self.functions.setdefault(item.value["name"], []).append(item)
            elif item.op == "declare":
                self.globals.append(item)
            elif item.op == "block":
                self.blocks.append(item.value)
        if "main" not in self.functions:
            raise GLSLError("no main function")

    def interface(self, qualifier):
        """The global variables with a qualifier: (name, kind, array, layout, qualifiers)."""
        out = []
        for item in self.globals:
            v = item.value
            quals = set(v["qualifiers"])
            if qualifier in quals or (qualifier == "in" and ("attribute" in quals if self.stage == "vertex" else
                                                              "varying" in quals)) or \
                    (qualifier == "out" and "varying" in quals and self.stage == "vertex"):
                for name, array, init in v["items"]:
                    out.append((name, v["kind"], array, v["layout"], quals))
        for block in self.blocks:
            if qualifier in block["qualifiers"] and qualifier in ("in", "out"):
                for name, kind, array, layout in block["members"]:
                    out.append((name, kind, array, layout, set(block["qualifiers"])))
        return out


# ------------------------------------------------------------------ execution
class Return(Exception):
    pass


class Frame:
    def __init__(self):
        self.returned = None    # mask of lanes that returned
        self.value = None


class Runtime:
    """A run of a program on n lanes. env: the variables of the scopes; textures: unit -> function
    (sampler kind, coords (n, k), lod or None, offset) -> (n, 4)."""

    def __init__(self, program: Program, n: int, textures=None):
        self.p = program
        self.n = n
        self.structs = program.structs
        self.scopes: List[Dict[str, Val]] = [{}]
        self.textures = textures or {}
        self.discarded = numpy.zeros(n, dtype=bool)
        self.loop_stack: List[dict] = []
        self.frames: List[Frame] = []
        self.constant_cache: Dict[int, Val] = {}
        self.images: Dict[int, object] = {}
        self.events = None          # a geometry shader: (emit / cut, stream, lanes, outputs)

    # variables --------------------------------------------------------------
    def lookup(self, name: str) -> Val:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        raise GLSLError(f"unknown variable {name}")

    def define(self, name: str, value: Val):
        self.scopes[-1][name] = value

    def has(self, name: str) -> bool:
        return any(name in scope for scope in self.scopes)

    def array_size(self, size_node) -> Optional[int]:
        if size_node is None:
            return None
        if size_node == -1:
            return -1
        value = self.eval(size_node, numpy.ones(self.n, bool))
        return int(numpy.asarray(value.data).reshape(-1)[0]) if self.n else int(numpy.asarray(value.data).reshape(-1)[0]) \
            if numpy.asarray(value.data).size else 0

    # values -----------------------------------------------------------------
    def constant(self, kind, value) -> Val:
        if kind == "float":
            return Val("float", numpy.full(self.n, float(value)))
        if kind == "bool":
            return Val("bool", numpy.full(self.n, bool(value)))
        return Val(kind, numpy.full(self.n, int(value), dtype=numpy.int64))

    def run_main(self):
        mask = numpy.ones(self.n, dtype=bool)
        for item in self.p.globals:
            v = item.value
            quals = set(v["qualifiers"])
            for name, array, init in v["items"]:
                if self.has(name) and ("uniform" in quals or "in" in quals or "attribute" in quals or "shared" in quals or
                                       ("varying" in quals and self.p.stage == "fragment")):
                    continue
                if self.has(name) and ("out" in quals or "varying" in quals):
                    continue
                size = self.array_size(array)
                value = zero(v["kind"], self.n, self.structs, size if size and size > 0 else None)
                if init is not None:
                    value = self.coerce(self.eval_init(init, v["kind"], mask), v["kind"])
                self.scopes[0][name] = value
        for block in self.p.blocks:
            if block["instance"] is None:
                for name, kind, array, layout in block["members"]:
                    if not self.has(name):
                        size = self.array_size(array)
                        self.scopes[0][name] = zero(kind, self.n, self.structs, size if size and size > 0 else None)
        self.call_function("main", [], mask)

    def eval_init(self, init, kind, mask):
        if init.op == "list":
            values = [self.eval_init(item, kind, mask) for item in init.args]
            if kind in self.structs:
                fields = self.structs[kind]
                return Val(kind, {f[0]: self.coerce(v, f[1]) for f, v in zip(fields, values)})
            return Val("array", values, len(values))
        return self.eval(init, mask)

    def coerce(self, value: Val, kind: str) -> Val:
        if value.kind == kind or kind in self.structs or value.kind == "array" or kind in SAMPLERS:
            if value.kind == "array" or kind in self.structs:
                return value
            return Val(kind, value.data) if kind in SAMPLERS else value
        if kind in SCALARS or kind in VECTORS:
            return self.construct(kind, [value])
        if kind in MATRICES:
            return self.construct(kind, [value])
        return value

    # functions --------------------------------------------------------------
    def call_function(self, name, args: List[Val], mask, arg_nodes=None):
        candidates = self.p.functions.get(name)
        if not candidates:
            raise GLSLError(f"unknown function {name}")
        function = self._overload(candidates, args)
        v = function.value
        self.scopes.append({})
        frame = Frame()
        frame.returned = numpy.zeros(self.n, dtype=bool)
        self.frames.append(frame)
        outs = []
        for (param, kind, array, quals), value in zip(v["params"], args):
            self.define(param, self.coerce(value, kind).copy() if value.kind != "array" else value.copy())
            if "out" in quals or "inout" in quals:
                outs.append(param)
            if "out" in quals and "inout" not in quals:
                self.define(param, zero(kind, self.n, self.structs) if value.kind != "array" else value.copy())
        saved_loops = self.loop_stack
        self.loop_stack = []
        try:
            self.exec(function.args[0], mask.copy())
        finally:
            self.loop_stack = saved_loops
        out_values = {param: self.lookup(param) for param in outs}
        self.scopes.pop()
        self.frames.pop()
        if arg_nodes is not None:
            for (param, kind, array, quals), node in zip(v["params"], arg_nodes):
                if param in out_values:
                    self.store(node, out_values[param], mask)
        if frame.value is None and v["kind"] != "void":
            return zero(v["kind"], self.n, self.structs)
        return frame.value

    def _overload(self, candidates, args):
        if len(candidates) == 1:
            return candidates[0]
        for candidate in candidates:
            params = candidate.value["params"]
            if len(params) == len(args) and all(p[1] == a.kind or (p[1] in SCALARS and a.kind in SCALARS)
                                                for p, a in zip(params, args)):
                return candidate
        for candidate in candidates:
            if len(candidate.value["params"]) == len(args):
                return candidate
        return candidates[0]

    # statements -------------------------------------------------------------
    def live(self, mask):
        alive = mask & ~self.discarded
        if self.frames:
            alive &= ~self.frames[-1].returned
        for loop in self.loop_stack:
            alive &= ~loop["broken"] & ~loop["continued"]
        return alive

    def exec(self, node: Node, mask):
        op = node.op
        mask = self.live(mask)
        if not mask.any() and op not in ("declare",):
            return
        if op == "block_stmt":
            self.scopes.append({})
            for statement in node.args:
                self.exec(statement, mask)
            self.scopes.pop()
        elif op == "expr":
            self.eval(node.args[0], mask)
        elif op == "declare":
            v = node.value
            for name, array, init in v["items"]:
                size = self.array_size(array)
                if init is not None:
                    value = self.eval_init(init, v["kind"], mask)
                    value = self.coerce(value, v["kind"]).copy()
                    if size == -1 and value.kind == "array":
                        size = value.length
                else:
                    value = zero(v["kind"], self.n, self.structs, size if size and size > 0 else None)
                self.define(name, value)
        elif op == "if":
            condition = self.truth(self.eval(node.args[0], mask))
            self.exec(node.args[1], mask & condition)
            if node.args[2] is not None:
                self.exec(node.args[2], mask & ~condition)
        elif op == "for":
            self.scopes.append({})
            init, condition, step, body = node.args
            self.exec(init, mask)
            loop = dict(broken=numpy.zeros(self.n, bool), continued=numpy.zeros(self.n, bool))
            self.loop_stack.append(loop)
            iterations = 0
            while True:
                active = self.live(mask)
                if condition is not None:
                    if condition.op == "cond_declare":
                        kind, name = condition.value
                        value = self.coerce(self.eval(condition.args[0], active), kind)
                        self.define(name, value)
                        result = self.truth(value)
                    else:
                        result = self.truth(self.eval(condition, active))
                    loop["broken"] |= active & ~result
                    active = active & result
                if not active.any() or iterations > 1 << 16:
                    break
                self.exec(body, active)
                loop["continued"][:] = False
                if step is not None:
                    self.eval(step, self.live(mask))
                iterations += 1
            self.loop_stack.pop()
            self.scopes.pop()
        elif op == "do":
            body, condition = node.args
            loop = dict(broken=numpy.zeros(self.n, bool), continued=numpy.zeros(self.n, bool))
            self.loop_stack.append(loop)
            iterations = 0
            while True:
                active = self.live(mask)
                if not active.any() or iterations > 1 << 16:
                    break
                self.exec(body, active)
                loop["continued"][:] = False
                active = self.live(mask)
                result = self.truth(self.eval(condition, active))
                loop["broken"] |= active & ~result
                iterations += 1
            self.loop_stack.pop()
        elif op == "switch":
            value = self.eval(node.args[0], mask)
            data = numpy.asarray(value.data).reshape(self.n)
            loop = dict(broken=numpy.zeros(self.n, bool), continued=numpy.zeros(self.n, bool), switch=True)
            self.loop_stack.append(loop)
            cases = node.value
            labels = [self.eval(case[1], mask).data for case in cases if case[0] == "case"]
            known = numpy.zeros(self.n, dtype=bool)
            for label in labels:
                known |= data == label
            entered = numpy.zeros(self.n, dtype=bool)
            for kind, label, statements in cases:
                hit = (data == self.eval(label, mask).data) if kind == "case" else ~known
                entered |= mask & hit
                for statement in statements:
                    self.exec(statement, entered)
            self.loop_stack.pop()
        elif op == "break":
            if self.loop_stack:
                self.loop_stack[-1]["broken"] |= mask
        elif op == "continue":
            for loop in reversed(self.loop_stack):
                if not loop.get("switch"):
                    loop["continued"] |= mask
                    break
        elif op == "discard":
            self.discarded |= mask
        elif op == "return":
            frame = self.frames[-1]
            if node.args[0] is not None:
                value = self.eval(node.args[0], mask)
                if frame.value is None:
                    frame.value = value.copy()
                else:
                    frame.value = self.select(mask, value, frame.value)
            frame.returned |= mask
        elif op in ("nop", "prototype"):
            pass
        else:
            raise GLSLError(f"statement {op}")

    def truth(self, value: Val):
        data = numpy.asarray(value.data)
        return (data != 0).reshape(self.n) if data.dtype != bool else data.reshape(self.n)

    def select(self, mask, new: Val, old: Val) -> Val:
        if new.kind == "array" or isinstance(new.data, dict):
            if new.kind == "array":
                return Val("array", [self.select(mask, a, b) for a, b in zip(new.data, old.data)], new.length)
            return Val(new.kind, {k: self.select(mask, new.data[k], old.data[k]) for k in new.data})
        if not isinstance(new.data, numpy.ndarray):
            return new
        shape = (self.n,) + (1,) * (new.data.ndim - 1)
        old_data = old.data if isinstance(old.data, numpy.ndarray) and old.data.shape == new.data.shape else \
            numpy.zeros_like(new.data)
        return Val(new.kind, numpy.where(mask.reshape(shape), new.data, old_data.astype(new.data.dtype)))

    # assignment -------------------------------------------------------------
    def _ref_of(self, node: Node, mask) -> Optional["Ref"]:
        """The memory a variable expression designates (a buffer block, a shared variable), or None."""
        if node.op == "name":
            if not self.has(node.value):
                return None
            value = self.lookup(node.value)
            return value.data if value.kind == "__ref" else None
        if node.op == "field":
            base = self._ref_of(node.args[0], mask)
            if base is None:
                return None
            if base.kind in base.structs and base.array is None:
                return _ref_field(base, node.value)
            indices = _swizzle(node.value)
            if len(indices) == 1 and base.kind in VECTORS:
                return _ref_index(base, numpy.full(self.n, indices[0]))
            return None
        if node.op == "index":
            base = self._ref_of(node.args[0], mask)
            if base is None:
                return None
            index = numpy.asarray(self.eval(node.args[1], mask).data).reshape(-1)
            return _ref_index(base, numpy.broadcast_to(index, (self.n,)) if index.size == 1 else index)
        return None

    def store(self, node: Node, value: Val, mask):
        if node.op in ("name", "field", "index"):
            target = self._ref_of(node, mask)
            if target is not None:
                ref_store(target, value, mask, self.n)
                return
            if node.op == "field":
                base = self._ref_of(node.args[0], mask)
                if base is not None:    # a swizzle of a vector in memory
                    data = as_columns(ref_load(base, self.n), self.n).copy()
                    indices = _swizzle(node.value)
                    source = as_columns(value, self.n)
                    if source.shape[1] == 1:
                        source = numpy.repeat(source, len(indices), axis=1)
                    for k, index in enumerate(indices):
                        data[:, index] = source[:, k]
                    ref_store(base, Val(base.kind, data), mask, self.n)
                    return
        if node.op == "name":
            name = node.value
            for scope in reversed(self.scopes):
                if name in scope:
                    old = scope[name]
                    value = self.coerce(value, old.kind) if old.kind != "array" else value
                    scope[name] = self.select(mask, value, old)
                    return
            raise GLSLError(f"assignment to an unknown variable {name}")
        if node.op == "field":
            base = self.eval(node.args[0], mask, lvalue=True)
            field = node.value
            if isinstance(base.data, dict):
                new = Val(base.kind, dict(base.data))
                new.data[field] = self.select(mask, self.coerce(value, base.data[field].kind), base.data[field])
                self.store(node.args[0], new, mask)
                return
            indices = _swizzle(field)
            data = base.data.copy()
            source = as_columns(value, self.n)
            if source.shape[1] == 1:
                source = numpy.repeat(source, len(indices), axis=1)
            for k, index in enumerate(indices):
                data[:, index] = numpy.where(mask, source[:, k], data[:, index])
            self.store(node.args[0], Val(base.kind, data), numpy.ones(self.n, bool))
            return
        if node.op == "index":
            base = self.eval(node.args[0], mask, lvalue=True)
            index = numpy.asarray(self.eval(node.args[1], mask).data).reshape(self.n).astype(numpy.int64)
            if base.kind == "array":
                items = [item for item in base.data]
                for k in numpy.unique(index[mask]) if mask.any() else []:
                    k = int(k)
                    if 0 <= k < len(items):
                        lanes = mask & (index == k)
                        items[k] = self.select(lanes, self.coerce(value, items[k].kind), items[k])
                self.store(node.args[0], Val("array", items, base.length), numpy.ones(self.n, bool))
                return
            data = base.data.copy()
            lanes = numpy.nonzero(mask)[0]
            if base.kind in MATRICES:
                column = as_columns(value, self.n)
                data[lanes, numpy.clip(index[lanes], 0, data.shape[1] - 1)] = column[lanes]
            else:
                source = numpy.asarray(value.data).reshape(self.n)
                data[lanes, numpy.clip(index[lanes], 0, data.shape[1] - 1)] = source[lanes]
            self.store(node.args[0], Val(base.kind, data), numpy.ones(self.n, bool))
            return
        raise GLSLError(f"assignment to {node.op}")

    # expressions ------------------------------------------------------------
    def eval(self, node: Node, mask, lvalue=False) -> Val:
        op = node.op
        if op == "const":
            kind, value = node.value
            return self.constant(kind, value)
        if op == "name":
            value = self.lookup(node.value)
            if value.kind == "__ref" and not lvalue:
                return ref_load(value.data, self.n)
            return value
        if op == "assign":
            if node.value == "=":
                value = self.eval(node.args[1], mask)
            else:
                value = self.binary(node.value[:-1], self.eval(node.args[0], mask), self.eval(node.args[1], mask))
            self.store(node.args[0], value, mask)
            return value
        if op == "binary":
            if node.value in ("&&", "||"):
                left = self.truth(self.eval(node.args[0], mask))
                right_mask = mask & left if node.value == "&&" else mask & ~left
                right = self.truth(self.eval(node.args[1], right_mask)) if right_mask.any() else numpy.zeros(self.n, bool)
                return Val("bool", (left & right) if node.value == "&&" else (left | right))
            return self.binary(node.value, self.eval(node.args[0], mask), self.eval(node.args[1], mask))
        if op == "unary":
            value = self.eval(node.args[0], mask)
            if node.value == "-":
                return Val(value.kind, -value.data)
            if node.value == "!":
                return Val(value.kind, ~self.truth(value) if value.data.ndim == 1 else ~value.data.astype(bool))
            if node.value == "~":
                return Val(value.kind, (~value.data) & (0xFFFFFFFF if base_of(value.kind) == "uint" else -1))
            return value
        if op in ("preinc", "postinc"):
            value = self.eval(node.args[0], mask)
            one = Val(base_of(value.kind), numpy.ones(self.n, dtype=value.data.dtype))
            new = self.binary("+" if node.value == "++" else "-", value, one)
            self.store(node.args[0], new, mask)
            return new if op == "preinc" else value
        if op == "ternary":
            condition = self.truth(self.eval(node.args[0], mask))
            a = self.eval(node.args[1], mask & condition) if (mask & condition).any() else None
            b = self.eval(node.args[2], mask & ~condition) if (mask & ~condition).any() else None
            if a is None:
                return b if b is not None else self.eval(node.args[1], mask)
            if b is None:
                return a
            return self.select(condition, a, b)
        if op == "comma":
            self.eval(node.args[0], mask)
            return self.eval(node.args[1], mask)
        if op == "field":
            base = self.eval(node.args[0], mask, lvalue=True)
            if base.kind == "__ref":
                ref = base.data
                if ref.kind in ref.structs and ref.array is None:
                    child = _ref_field(ref, node.value)
                    return Val("__ref", child) if lvalue else ref_load(child, self.n)
                base = ref_load(ref, self.n)
            if isinstance(base.data, dict):
                return base.data[node.value]
            indices = _swizzle(node.value)
            data = base.data if base.data.ndim > 1 else base.data[:, None]
            picked = data[:, indices]
            kind = vector_kind(base_of(base.kind), len(indices))
            return Val(kind, picked[:, 0] if len(indices) == 1 else picked)
        if op == "index":
            base = self.eval(node.args[0], mask, lvalue=True)
            index = numpy.asarray(self.eval(node.args[1], mask).data).reshape(-1)
            if base.kind == "__ref":
                child = _ref_index(base.data, numpy.broadcast_to(index, (self.n,)) if index.size == 1 else index)
                return Val("__ref", child) if lvalue else ref_load(child, self.n)
            if base.kind == "array":
                index = index.astype(numpy.int64)
                if len(numpy.unique(index)) <= 1:
                    k = int(index[0]) if len(index) else 0
                    return base.data[min(max(k, 0), len(base.data) - 1)]
                result = base.data[0].copy()
                for k in numpy.unique(index):
                    lanes = index == k
                    item = base.data[min(max(int(k), 0), len(base.data) - 1)]
                    result = self.select(lanes, item, result)
                return result
            index = numpy.clip(index.astype(numpy.int64), 0, base.data.shape[1] - 1)
            rows = numpy.arange(self.n)
            if base.kind in MATRICES:
                c, r = MATRICES[base.kind]
                return Val(vector_kind("float", r), base.data[rows, index])
            return Val(base_of(base.kind), base.data[rows, index])
        if op == "length":
            base = self.eval(node.args[0], mask, lvalue=True)
            if base.kind == "__ref":
                return self.constant("int", _ref_length(base.data))
            if base.kind == "array":
                return self.constant("int", base.length)
            if base.kind in MATRICES:
                return self.constant("int", MATRICES[base.kind][0])
            return self.constant("int", base.data.shape[1] if base.data.ndim > 1 else 1)
        if op == "call":
            return self.call(node, mask)
        if op == "array_constructor":
            kind, size = node.value
            values = [self.coerce(self.eval(a, mask), kind) for a in node.args]
            return Val("array", values, len(values))
        if op == "list":
            values = [self.eval(a, mask) for a in node.args]
            return Val("array", values, len(values))
        raise GLSLError(f"expression {op}")

    def binary(self, op, a: Val, b: Val) -> Val:
        if op in ("==", "!="):
            equal = _equal(a, b, self.n)
            return Val("bool", equal if op == "==" else ~equal)
        if op == "*" and (a.kind in MATRICES or b.kind in MATRICES):
            return self.matrix_multiply(a, b)
        base = _result_base(a, b)
        x, y = _broadcast(a, b, base, self.n)
        if op in ("<", ">", "<=", ">="):
            result = {"<": numpy.less, ">": numpy.greater, "<=": numpy.less_equal, ">=": numpy.greater_equal}[op](x, y)
            return Val("bool", result)
        if op == "^^":
            return Val("bool", self.truth(a) ^ self.truth(b))
        with numpy.errstate(all="ignore"):
            if op == "+":
                result = x + y
            elif op == "-":
                result = x - y
            elif op == "*":
                result = x * y
            elif op == "/":
                if base == "float":
                    result = x / y
                else:
                    safe = numpy.where(y == 0, 1, y)
                    result = numpy.where(y == 0, 0, numpy.trunc(x / safe).astype(numpy.int64))
            elif op == "%":
                safe = numpy.where(y == 0, 1, y)
                result = numpy.where(y == 0, 0, numpy.fmod(x, safe)) if base != "float" else numpy.mod(x, safe)
            elif op == "<<":
                result = x << (y & 31)
            elif op == ">>":
                result = x >> (y & 31)
            elif op == "&":
                result = x & y
            elif op == "|":
                result = x | y
            elif op == "^":
                result = x ^ y
            else:
                raise GLSLError(f"operator {op}")
        if base == "uint":
            result = numpy.asarray(result).astype(numpy.int64) & 0xFFFFFFFF
        elif base == "int":
            result = ((numpy.asarray(result).astype(numpy.int64) + 0x80000000) & 0xFFFFFFFF) - 0x80000000
        kind = a.kind if size_of(a.kind) >= size_of(b.kind) and a.kind not in SAMPLERS else b.kind
        if kind in MATRICES:
            return Val(kind, result.reshape(self.n, *MATRICES[kind]))
        return Val(vector_kind(base, size_of(kind)) if kind not in MATRICES else kind, result if result.ndim > 1 and
                   result.shape[1] > 1 else result.reshape(self.n))

    def matrix_multiply(self, a: Val, b: Val) -> Val:
        if a.kind in MATRICES and b.kind in MATRICES:     # (c1, r1) x (c2, r2): column j = A * B[:, j]
            ma, mb = a.data, b.data          # (n, cols, rows)
            product = numpy.einsum("nkr,nck->ncr", ma, mb)
            return Val(f"mat{product.shape[1]}" if product.shape[1] == product.shape[2] else
                       f"mat{product.shape[1]}x{product.shape[2]}", product)
        if a.kind in MATRICES:                             # matrix * vector
            vector = as_columns(b, self.n).astype(numpy.float64)
            if vector.shape[1] == 1 and b.kind in SCALARS:
                return Val(a.kind, a.data * vector[:, :, None])
            result = numpy.einsum("ncr,nc->nr", a.data, vector)
            return Val(vector_kind("float", result.shape[1]), result)
        vector = as_columns(a, self.n).astype(numpy.float64)   # vector * matrix (row vector)
        if vector.shape[1] == 1 and a.kind in SCALARS:
            return Val(b.kind, b.data * vector[:, :, None])
        result = numpy.einsum("nr,ncr->nc", vector, b.data)
        return Val(vector_kind("float", result.shape[1]), result)

    # constructors and built-in functions ----------------------------------------
    def construct(self, kind: str, args: List[Val]) -> Val:
        if kind in self.structs:
            fields = self.structs[kind]
            return Val(kind, {f[0]: self.coerce(v, f[1]) for f, v in zip(fields, args)})
        if kind in MATRICES:
            c, r = MATRICES[kind]
            if len(args) == 1 and args[0].kind in MATRICES:
                source = args[0].data
                out = numpy.zeros((self.n, c, r))
                for k in range(min(c, r)):
                    out[:, k, k] = 1
                sc, sr = source.shape[1], source.shape[2]
                out[:, :min(c, sc), :min(r, sr)] = source[:, :min(c, sc), :min(r, sr)]
                return Val(kind, out)
            if len(args) == 1 and size_of(args[0].kind) == 1:
                value = convert(args[0], "float")
                out = numpy.zeros((self.n, c, r))
                for k in range(min(c, r)):
                    out[:, k, k] = value
                return Val(kind, out)
            flat = numpy.concatenate([as_columns(Val(a.kind, convert(a, "float")) if a.kind not in MATRICES else a, self.n)
                                      for a in args], axis=1)
            return Val(kind, flat[:, :c * r].reshape(self.n, c, r))
        base = base_of(kind)
        count = size_of(kind)
        if len(args) == 1 and size_of(args[0].kind) == 1 and args[0].kind not in MATRICES:
            value = convert(args[0], base)
            if count == 1:
                return Val(kind, value)
            return Val(kind, numpy.repeat(value[:, None], count, axis=1))
        parts = [as_columns(Val(a.kind, convert(a, base)), self.n) if a.kind not in MATRICES else a.data.reshape(self.n, -1)
                 for a in args]
        flat = numpy.concatenate(parts, axis=1)[:, :count]
        if base == "bool":
            flat = flat != 0
        return Val(kind, flat[:, 0] if count == 1 else flat)

    def call(self, node: Node, mask) -> Val:
        name = node.value
        if name in ATOMICS and name not in self.p.functions:
            target = self._ref_of(node.args[0], mask)
            if target is None:
                raise GLSLError(f"{name} of a variable that is not in memory")
            operands = [self.eval(a, mask) for a in node.args[1:]]
            return ref_atomic(self, name, target, operands, mask)
        if name.startswith("image") and name not in self.p.functions:
            return image_call(self, name, [self.eval(a, mask) for a in node.args], mask)
        if name in ("EmitVertex", "EmitStreamVertex", "EndPrimitive", "EndStreamPrimitive"):
            stream = int(numpy.asarray(self.eval(node.args[0], mask).data).reshape(-1)[0]) if node.args else 0
            if self.events is not None:
                lanes = numpy.nonzero(self.live(mask))[0]
                if name.startswith("Emit"):
                    self.events.append(("emit", stream, lanes, {k: _rows(v, lanes) for k, v in emit_outputs(self).items()}))
                else:
                    self.events.append(("cut", stream, lanes, None))
            return Val("void", None)
        if name in ("barrier", "memoryBarrier", "memoryBarrierShared", "memoryBarrierBuffer", "memoryBarrierImage",
                    "memoryBarrierAtomicCounter", "groupMemoryBarrier"):
            return Val("void", None)
        if name in self.p.functions:
            args = [self.eval(a, mask) for a in node.args]
            return self.call_function(name, args, mask, node.args)
        if name in SCALARS or name in VECTORS or name in MATRICES or name in self.structs:
            return self.construct(name, [self.eval(a, mask) for a in node.args])
        args = [self.eval(a, mask) for a in node.args]
        function = BUILTINS.get(name)
        if function is None:
            if name.startswith("texture") or name in ("shadow2D", "shadow2DProj", "shadow1D", "texelFetch",
                                                      "texelFetchOffset"):
                return self.texture(name, args)
            raise GLSLError(f"unknown function {name}")
        return function(self, args)

    def texture(self, name, args) -> Val:
        sampler = args[0]
        unit = int(sampler.data) if not isinstance(sampler.data, numpy.ndarray) else int(numpy.asarray(sampler.data).reshape(-1)[0])
        function = self.textures.get(unit)
        coords = as_columns(args[1], self.n).astype(numpy.float64)
        lod = None
        offset = (0, 0)
        extra = {}
        if name in ("textureSize",):
            size = function(sampler.kind, None, "size", None) if function is not None else (0, 0)
            values = numpy.array(size[:2] if "1D" not in sampler.kind else size[:1], dtype=numpy.int64)
            return Val(vector_kind("int", len(values)), numpy.broadcast_to(values, (self.n, len(values))).copy()
                       if len(values) > 1 else numpy.full(self.n, int(values[0])))
        if "Proj" in name:
            last = coords[:, -1:]
            coords = coords / numpy.where(last == 0, 1, last)
        if "Lod" in name and len(args) > 2:
            lod = as_columns(args[2], self.n)[:, 0]
        if "Grad" in name and len(args) > 3:
            extra["gradients"] = (as_columns(args[2], self.n).astype(numpy.float64),
                                  as_columns(args[3], self.n).astype(numpy.float64))
        if "Offset" in name and len(args) > 2:
            offset = tuple(int(v) for v in as_columns(args[-1 if "Grad" not in name else 4], self.n)[0][:2])
        if name in ("texture", "texture2D", "texture1D", "texture3D", "textureCube", "textureProj", "texture2DProj") \
                and len(args) > 2 and "Shadow" not in sampler.kind:
            extra["bias"] = as_columns(args[2], self.n)[:, 0].astype(numpy.float64)
        if name.startswith("texelFetch"):
            lod = as_columns(args[2], self.n)[:, 0] if len(args) > 2 else None
            result = function(sampler.kind, coords, "fetch", lod) if function is not None else numpy.zeros((self.n, 4))
        else:
            result = function(sampler.kind, coords, offset, lod, **extra) if function is not None \
                else numpy.zeros((self.n, 4))
        result = numpy.asarray(result, dtype=numpy.float64)
        if "Shadow" in sampler.kind or name.startswith("shadow"):
            reference = coords[:, 2] if coords.shape[1] > 2 else coords[:, -1]
            passed = (reference <= result[:, 0]).astype(numpy.float64)
            if name.startswith("shadow"):
                return Val("vec4", numpy.repeat(passed[:, None], 4, 1))
            return Val("float", passed)
        if sampler.kind.startswith("i"):
            return Val("ivec4", numpy.round(result).astype(numpy.int64))
        if sampler.kind.startswith("u"):
            return Val("uvec4", numpy.round(result).astype(numpy.int64))
        return Val("vec4", result)


def _swizzle(text):
    table = {"x": 0, "y": 1, "z": 2, "w": 3, "r": 0, "g": 1, "b": 2, "a": 3, "s": 0, "t": 1, "p": 2, "q": 3}
    return [table[c] for c in text]


def _result_base(a: Val, b: Val) -> str:
    bases = {base_of(a.kind), base_of(b.kind)}
    if "float" in bases or a.kind in MATRICES or b.kind in MATRICES:
        return "float"
    if "uint" in bases:
        return "uint"
    if "int" in bases:
        return "int"
    return "bool"


def _broadcast(a: Val, b: Val, base, n):
    x, y = convert(a, base), convert(b, base)
    if a.kind in MATRICES:
        x = x.reshape(n, -1)
    if b.kind in MATRICES:
        y = y.reshape(n, -1)
    if x.ndim == 1 and y.ndim > 1:
        x = x[:, None]
    elif y.ndim == 1 and x.ndim > 1:
        y = y[:, None]
    return x, y


def _equal(a: Val, b: Val, n):
    if a.kind == "array":
        out = numpy.ones(n, dtype=bool)
        for x, y in zip(a.data, b.data):
            out &= _equal(x, y, n)
        return out
    if isinstance(a.data, dict):
        out = numpy.ones(n, dtype=bool)
        for k in a.data:
            out &= _equal(a.data[k], b.data[k], n)
        return out
    base = _result_base(a, b)
    x, y = _broadcast(a, b, base, n)
    result = x == y
    return result.all(axis=1) if result.ndim > 1 else result


# ------------------------------------------------------------------ built-in functions
def _f(v: Val):
    return convert(v, "float")


def _same(kind_of: Val, data):
    base = "float"
    return Val(vector_kind(base, size_of(kind_of.kind)) if kind_of.kind not in MATRICES else kind_of.kind, data)


def _unary(function):
    def run(rt, args):
        with numpy.errstate(all="ignore"):
            return _same(args[0], function(_f(args[0])))
    return run


def _genbinary(function):
    def run(rt, args):
        a, b = args
        base = _result_base(a, b)
        x, y = _broadcast(a, b, base, rt.n)
        with numpy.errstate(all="ignore"):
            result = function(x, y)
        kind = a if size_of(a.kind) >= size_of(b.kind) else b
        if base != "float":
            return Val(vector_kind(base, size_of(kind.kind)), result if result.ndim > 1 else result.reshape(rt.n))
        return Val(vector_kind("float", size_of(kind.kind)), result if result.ndim > 1 else result.reshape(rt.n))
    return run


def _clamp(rt, args):
    a, low, high = args
    base = _result_base(a, low)
    x, l = _broadcast(a, low, base, rt.n)
    _, h = _broadcast(a, high, base, rt.n)
    return Val(vector_kind(base, size_of(a.kind)), numpy.minimum(numpy.maximum(x, l), h))


def _mix(rt, args):
    a, b, t = args
    if base_of(t.kind) == "bool":
        x, y = _broadcast(a, b, "float", rt.n)
        condition = t.data if t.data.ndim > 1 else t.data[:, None] if x.ndim > 1 else t.data
        return Val(a.kind, numpy.where(condition, y, x))
    x, y = _broadcast(a, b, "float", rt.n)
    w = _f(t)
    if w.ndim == 1 and x.ndim > 1:
        w = w[:, None]
    return Val(vector_kind("float", max(size_of(a.kind), size_of(b.kind))), x * (1 - w) + y * w)


def _step(rt, args):
    edge, x = args
    e, v = _broadcast(edge, x, "float", rt.n)
    return Val(vector_kind("float", max(size_of(edge.kind), size_of(x.kind))), (v >= e).astype(numpy.float64))


def _smoothstep(rt, args):
    e0, e1, x = args
    a, v = _broadcast(e0, x, "float", rt.n)
    b, _ = _broadcast(e1, x, "float", rt.n)
    with numpy.errstate(all="ignore"):
        t = numpy.clip((v - a) / (b - a), 0, 1)
    return Val(vector_kind("float", size_of(x.kind)), t * t * (3 - 2 * t))


def _vec_data(v: Val, n):
    data = _f(v)
    return data if data.ndim > 1 else data[:, None]


def _length(rt, args):
    return Val("float", numpy.sqrt((_vec_data(args[0], rt.n) ** 2).sum(1)))


def _distance(rt, args):
    return Val("float", numpy.sqrt(((_vec_data(args[0], rt.n) - _vec_data(args[1], rt.n)) ** 2).sum(1)))


def _dot(rt, args):
    return Val("float", (_vec_data(args[0], rt.n) * _vec_data(args[1], rt.n)).sum(1))


def _normalize(rt, args):
    v = _vec_data(args[0], rt.n)
    length = numpy.sqrt((v ** 2).sum(1, keepdims=True))
    with numpy.errstate(all="ignore"):
        result = v / length
    return Val(args[0].kind if args[0].kind not in SCALARS else "float", result if result.shape[1] > 1 else result[:, 0])


def _cross(rt, args):
    return Val("vec3", numpy.cross(_vec_data(args[0], rt.n), _vec_data(args[1], rt.n)))


def _reflect(rt, args):
    i, n = _vec_data(args[0], rt.n), _vec_data(args[1], rt.n)
    return Val(args[0].kind, i - 2 * (n * i).sum(1, keepdims=True) * n)


def _refract(rt, args):
    i, n = _vec_data(args[0], rt.n), _vec_data(args[1], rt.n)
    eta = _f(args[2]).reshape(rt.n, 1)
    d = (n * i).sum(1, keepdims=True)
    k = 1 - eta * eta * (1 - d * d)
    with numpy.errstate(all="ignore"):
        result = numpy.where(k < 0, 0, eta * i - (eta * d + numpy.sqrt(numpy.maximum(k, 0))) * n)
    return Val(args[0].kind, result)


def _faceforward(rt, args):
    n, i, ref = (_vec_data(a, rt.n) for a in args)
    return Val(args[0].kind, numpy.where((ref * i).sum(1, keepdims=True) < 0, n, -n))


def _compare(function):
    def run(rt, args):
        x, y = _broadcast(args[0], args[1], _result_base(args[0], args[1]), rt.n)
        return Val(vector_kind("bool", size_of(args[0].kind)), function(x, y))
    return run


def _any(rt, args):
    return Val("bool", numpy.asarray(args[0].data).astype(bool).any(axis=1))


def _all(rt, args):
    return Val("bool", numpy.asarray(args[0].data).astype(bool).all(axis=1))


def _not(rt, args):
    return Val(args[0].kind, ~numpy.asarray(args[0].data).astype(bool))


def _transpose(rt, args):
    m = args[0].data
    c, r = m.shape[1], m.shape[2]
    return Val(f"mat{r}" if r == c else f"mat{r}x{c}", m.transpose(0, 2, 1))


def _determinant(rt, args):
    return Val("float", numpy.linalg.det(args[0].data.transpose(0, 2, 1)))


def _inverse(rt, args):
    m = args[0].data.transpose(0, 2, 1)
    with numpy.errstate(all="ignore"):
        try:
            inverse = numpy.linalg.inv(m)
        except numpy.linalg.LinAlgError:
            inverse = numpy.linalg.pinv(m)
    return Val(args[0].kind, inverse.transpose(0, 2, 1))


def _outer(rt, args):
    c, r = _vec_data(args[1], rt.n), _vec_data(args[0], rt.n)
    product = c[:, :, None] * r[:, None, :]
    cols, rows = product.shape[1], product.shape[2]
    return Val(f"mat{cols}" if cols == rows else f"mat{cols}x{rows}", product)


def _matrix_comp(rt, args):
    return Val(args[0].kind, args[0].data * args[1].data)


def _min_max(function):
    def run(rt, args):
        a, b = args
        base = _result_base(a, b)
        x, y = _broadcast(a, b, base, rt.n)
        result = function(x, y)
        kind = a if size_of(a.kind) >= size_of(b.kind) else b
        return Val(vector_kind(base, size_of(kind.kind)), result if result.ndim > 1 else result.reshape(rt.n))
    return run


def _abs(rt, args):
    v = args[0]
    return Val(v.kind, numpy.abs(v.data))


def _sign(rt, args):
    v = args[0]
    return Val(v.kind, numpy.sign(v.data))


def _bits(target):
    def run(rt, args):
        v = args[0]
        if target == "float":
            data = (numpy.asarray(v.data).astype(numpy.int64) & 0xFFFFFFFF).astype(numpy.uint32).view(numpy.float32)
            return Val(vector_kind("float", size_of(v.kind)), data.astype(numpy.float64))
        data = numpy.asarray(v.data).astype(numpy.float32).view(numpy.int32 if target == "int" else numpy.uint32)
        return Val(vector_kind(target, size_of(v.kind)), data.astype(numpy.int64))
    return run


def _modf(rt, args):
    value = _f(args[0])
    whole = numpy.trunc(value)
    return Val(args[0].kind, value - whole)


def _ftransform(rt, args):
    matrix = rt.lookup("gl_ModelViewProjectionMatrix")
    return rt.matrix_multiply(matrix, rt.lookup("gl_Vertex"))


def _zero_like(rt, args):
    return Val(args[0].kind, numpy.zeros_like(_f(args[0])))


def _derivative(axis, fine=False, width=False):
    def function(rt, args):
        from .raster import derivative
        value = _f(args[0])
        if width:
            return Val(args[0].kind, numpy.abs(derivative(value, "x", fine)) + numpy.abs(derivative(value, "y", fine)))
        return Val(args[0].kind, derivative(value, axis, fine))
    return function


def _atan(rt, args):
    if len(args) == 2:
        y, x = _broadcast(args[0], args[1], "float", rt.n)
        return Val(args[0].kind, numpy.arctan2(y, x))
    return _same(args[0], numpy.arctan(_f(args[0])))


BUILTINS = {
    "radians": _unary(numpy.radians), "degrees": _unary(numpy.degrees), "sin": _unary(numpy.sin), "cos": _unary(numpy.cos),
    "tan": _unary(numpy.tan), "asin": _unary(numpy.arcsin), "acos": _unary(numpy.arccos), "atan": _atan,
    "sinh": _unary(numpy.sinh), "cosh": _unary(numpy.cosh), "tanh": _unary(numpy.tanh), "asinh": _unary(numpy.arcsinh),
    "acosh": _unary(numpy.arccosh), "atanh": _unary(numpy.arctanh), "pow": _genbinary(numpy.power),
    "exp": _unary(numpy.exp), "log": _unary(numpy.log), "exp2": _unary(numpy.exp2), "log2": _unary(numpy.log2),
    "sqrt": _unary(numpy.sqrt), "inversesqrt": _unary(lambda x: 1 / numpy.sqrt(x)), "abs": _abs, "sign": _sign,
    "floor": _unary(numpy.floor), "trunc": _unary(numpy.trunc), "round": _unary(numpy.round),
    "roundEven": _unary(numpy.round), "ceil": _unary(numpy.ceil), "fract": _unary(lambda x: x - numpy.floor(x)),
    "mod": _genbinary(lambda x, y: x - y * numpy.floor(x / y)), "min": _min_max(numpy.minimum),
    "max": _min_max(numpy.maximum), "clamp": _clamp, "mix": _mix, "step": _step, "smoothstep": _smoothstep,
    "isnan": lambda rt, a: Val(vector_kind("bool", size_of(a[0].kind)), numpy.isnan(_f(a[0]))),
    "isinf": lambda rt, a: Val(vector_kind("bool", size_of(a[0].kind)), numpy.isinf(_f(a[0]))),
    "floatBitsToInt": _bits("int"), "floatBitsToUint": _bits("uint"), "intBitsToFloat": _bits("float"),
    "uintBitsToFloat": _bits("float"), "length": _length, "distance": _distance, "dot": _dot, "cross": _cross,
    "normalize": _normalize, "faceforward": _faceforward, "reflect": _reflect, "refract": _refract,
    "matrixCompMult": _matrix_comp, "outerProduct": _outer, "transpose": _transpose, "determinant": _determinant,
    "inverse": _inverse, "lessThan": _compare(numpy.less), "lessThanEqual": _compare(numpy.less_equal),
    "greaterThan": _compare(numpy.greater), "greaterThanEqual": _compare(numpy.greater_equal),
    "equal": _compare(numpy.equal), "notEqual": _compare(numpy.not_equal), "any": _any, "all": _all, "not": _not,
    "dFdx": _derivative("x", True), "dFdy": _derivative("y", True), "fwidth": _derivative(None, True, True),
    "dFdxFine": _derivative("x", True), "dFdyFine": _derivative("y", True), "dFdxCoarse": _derivative("x"),
    "dFdyCoarse": _derivative("y"), "fwidthFine": _derivative(None, True, True),
    "fwidthCoarse": _derivative(None, False, True),
    "ftransform": _ftransform, "modf": _modf, "fma": lambda rt, a: Val(a[0].kind, _f(a[0]) * _f(a[1]) + _f(a[2])),
}


# ------------------------------------------------------------------ what OpenGL uses
COMPAT_VERTEX = {"gl_Vertex": "vec4", "gl_Normal": "vec3", "gl_Color": "vec4", "gl_SecondaryColor": "vec4",
                 "gl_FogCoord": "float", **{f"gl_MultiTexCoord{k}": "vec4" for k in range(8)}}
COMPAT_MATRICES = ["gl_ModelViewMatrix", "gl_ProjectionMatrix", "gl_ModelViewProjectionMatrix", "gl_NormalMatrix",
                   "gl_ModelViewMatrixInverse", "gl_ProjectionMatrixInverse", "gl_ModelViewProjectionMatrixInverse"]
VERTEX_OUTPUTS = {"gl_Position": "vec4", "gl_PointSize": "float", "gl_FrontColor": "vec4", "gl_BackColor": "vec4",
                  "gl_FrontSecondaryColor": "vec4", "gl_BackSecondaryColor": "vec4", "gl_FogFragCoord": "float",
                  "gl_ClipVertex": "vec4"}


def run_vertex(program: Program, n: int, inputs: Dict[str, Val], uniforms: Dict[str, Val], textures) -> Runtime:
    """inputs: the attributes (and gl_VertexID / gl_InstanceID / the compatibility ones); uniforms: the
    values of the uniforms (and of the variables of the uniform blocks)."""
    rt = Runtime(program, n, textures)
    for name, value in uniforms.items():
        rt.scopes[0][name] = value
    for name, value in inputs.items():
        rt.scopes[0][name] = value
    for name, kind in VERTEX_OUTPUTS.items():
        rt.scopes[0].setdefault(name, zero(kind, n))
    rt.scopes[0].setdefault("gl_TexCoord", zero("vec4", n, None, 8))
    rt.scopes[0].setdefault("gl_ClipDistance", zero("float", n, None, 8))
    rt.scopes[0]["gl_PointSize"] = Val("float", numpy.ones(n))
    for name, kind in COMPAT_VERTEX.items():
        rt.scopes[0].setdefault(name, zero(kind, n))
    for item in program.interface("out"):
        name, kind, array, layout, quals = item
        if name not in rt.scopes[0]:
            size = rt.array_size(array)
            rt.scopes[0][name] = zero(kind, n, program.structs, size if size and size > 0 else None)
    for block in program.blocks:
        if "out" in block["qualifiers"] and block["instance"]:
            fields = {name: zero(kind, n, program.structs) for name, kind, array, layout in block["members"]}
            rt.scopes[0][block["instance"]] = Val("__block", fields)
    rt.run_main()
    return rt


def run_fragment(program: Program, n: int, inputs: Dict[str, Val], uniforms: Dict[str, Val], textures) -> Runtime:
    rt = Runtime(program, n, textures)
    for name, value in uniforms.items():
        rt.scopes[0][name] = value
    for name, value in inputs.items():
        rt.scopes[0][name] = value
    rt.scopes[0].setdefault("gl_FragColor", zero("vec4", n))
    rt.scopes[0].setdefault("gl_FragData", zero("vec4", n, None, 8))
    rt.scopes[0].setdefault("gl_FragDepth", zero("float", n))
    rt.scopes[0].setdefault("gl_TexCoord", zero("vec4", n, None, 8))
    for item in program.interface("out"):
        name, kind, array, layout, quals = item
        if name not in rt.scopes[0]:
            size = rt.array_size(array)
            rt.scopes[0][name] = zero(kind, n, program.structs, size if size and size > 0 else None)
    for item in program.interface("in"):
        name, kind, array, layout, quals = item
        if name not in rt.scopes[0]:
            size = rt.array_size(array)
            rt.scopes[0][name] = zero(kind, n, program.structs, size if size and size > 0 else None)
    for block in program.blocks:
        if "in" in block["qualifiers"] and block["instance"] and block["instance"] not in rt.scopes[0]:
            fields = {name: zero(kind, n, program.structs) for name, kind, array, layout in block["members"]}
            rt.scopes[0][block["instance"]] = Val("__block", fields)
    rt.run_main()
    return rt


# ------------------------------------------------------------------ memory: buffer blocks, shared variables
class Ref:
    """A variable in memory (a buffer block, a shared variable, an atomic counter): the bytes (numpy uint8,
    shared by the lanes), the byte offset of each lane, the type, the array length (None: not an array, -1:
    the runtime-sized last member), the layout (std430 or std140)."""

    __slots__ = ("memory", "offset", "kind", "array", "std430", "structs")

    def __init__(self, memory, offset, kind, array=None, std430=True, structs=None):
        self.memory, self.offset, self.kind, self.array = memory, offset, kind, array
        self.std430, self.structs = std430, structs or {}


def _round(value, align):
    return (value + align - 1) // align * align


def layout_of(kind: str, structs, std430=True, array=None):
    """(size, alignment) of a type in a std430 / std140 block."""
    if array is not None:
        stride = array_stride(kind, structs, std430)
        size, align = layout_of(kind, structs, std430)
        if not std430:
            align = _round(align, 16)
        return stride * max(array, 0), align
    if kind in structs:
        offset, align = 0, 1 if std430 else 16
        for field, field_kind, field_array in structs[kind]:
            count = _const_count(field_array)
            size, field_align = layout_of(field_kind, structs, std430, count)
            align = max(align, field_align)
            offset = _round(offset, field_align) + size
        return _round(offset, align), align
    if kind in MATRICES:
        c, r = MATRICES[kind]
        column = array_stride(vector_kind("float", r), structs, std430)
        return column * c, column if std430 else 16
    if kind in VECTORS:
        k = VECTORS[kind][1]
        return 4 * k, 8 if k == 2 else 16
    return 4, 4


def array_stride(kind: str, structs, std430=True) -> int:
    size, align = layout_of(kind, structs, std430)
    stride = _round(size, align)
    return stride if std430 else _round(stride, 16)


def _const_count(node):
    if node is None:
        return None
    if node == -1:
        return -1
    if isinstance(node, int):
        return node
    if getattr(node, "op", None) == "const":
        return int(node.value[1])
    return None


def member_offsets(members, structs, std430=True):
    """[(name, kind, array, offset)] of the members of a block (array: count, -1 for a runtime array)."""
    out, offset = [], 0
    for item in members:
        name, kind, array = item[0], item[1], item[2]
        layout = item[3] if len(item) > 3 else {}
        count = _const_count(array)
        size, align = layout_of(kind, structs, std430, count if count and count > 0 else None)
        if count == -1:
            size = 0
        explicit = (layout or {}).get("offset")
        offset = _const_count(explicit) if explicit is not None else _round(offset, align)
        out.append((name, kind, count, offset))
        offset += size
    return out


def _ref_field(ref: Ref, name: str) -> Ref:
    for field, kind, count, offset in member_offsets(ref.structs[ref.kind], ref.structs, ref.std430):
        if field == name:
            return Ref(ref.memory, ref.offset + offset, kind, count, ref.std430, ref.structs)
    raise GLSLError(f"no member {name}")


def _ref_index(ref: Ref, index) -> Ref:
    index = numpy.asarray(index).astype(numpy.int64)
    if ref.array is not None:
        stride = array_stride(ref.kind, ref.structs, ref.std430)
        return Ref(ref.memory, ref.offset + index * stride, ref.kind, None, ref.std430, ref.structs)
    if ref.kind in MATRICES:
        c, r = MATRICES[ref.kind]
        column = array_stride(vector_kind("float", r), ref.structs, ref.std430)
        return Ref(ref.memory, ref.offset + index * column, vector_kind("float", r), None, ref.std430, ref.structs)
    if ref.kind in VECTORS:
        return Ref(ref.memory, ref.offset + index * 4, VECTORS[ref.kind][0], None, ref.std430, ref.structs)
    raise GLSLError("index of a scalar")


def _ref_length(ref: Ref) -> int:
    if ref.array is None:
        return 0
    if ref.array >= 0:
        return ref.array
    stride = array_stride(ref.kind, ref.structs, ref.std430)
    start = int(numpy.asarray(ref.offset).reshape(-1)[0]) if numpy.asarray(ref.offset).size else 0
    return max(0, (len(ref.memory) - start) // max(stride, 1))


def _words(ref: Ref, n, count):
    """The 32-bit words (n, count) of a lane's value, and the lanes inside the memory."""
    offset = numpy.broadcast_to(numpy.asarray(ref.offset, dtype=numpy.int64), (n,))
    inside = (offset >= 0) & (offset + 4 * count <= len(ref.memory))
    safe = numpy.where(inside, offset, 0)
    index = safe[:, None] + 4 * numpy.arange(count)
    words = numpy.zeros((n, count), dtype=numpy.uint32)
    if len(ref.memory) >= 4:
        raw = ref.memory[(index[:, :, None] + numpy.arange(4)).clip(0, len(ref.memory) - 1)]
        words = raw.astype(numpy.uint32) @ numpy.array([1, 1 << 8, 1 << 16, 1 << 24], dtype=numpy.uint32)
    return words, inside


def ref_load(ref: Ref, n: int) -> Val:
    if ref.array is not None:
        length = _ref_length(ref)
        return Val("array", [ref_load(_ref_index(ref, numpy.full(n, k)), n) for k in range(length)], length)
    if ref.kind in ref.structs:
        return Val(ref.kind, {name: ref_load(Ref(ref.memory, ref.offset + offset, kind, count, ref.std430, ref.structs), n)
                              for name, kind, count, offset in member_offsets(ref.structs[ref.kind], ref.structs, ref.std430)})
    if ref.kind in MATRICES:
        c, r = MATRICES[ref.kind]
        columns = [ref_load(_ref_index(ref, numpy.full(n, k)), n).data for k in range(c)]
        return Val(ref.kind, numpy.stack(columns, axis=1))
    base = base_of(ref.kind)
    count = size_of(ref.kind)
    words, inside = _words(ref, n, count)
    words = numpy.where(inside[:, None], words, 0).astype(numpy.uint32)
    if base == "float":
        data = words.view(numpy.float32).astype(numpy.float64)
    elif base == "int":
        data = words.view(numpy.int32).astype(numpy.int64)
    elif base == "uint":
        data = words.astype(numpy.int64)
    else:
        data = words != 0
    return Val(ref.kind, data[:, 0] if count == 1 else data)


def ref_store(ref: Ref, value: Val, mask, n: int):
    lanes = numpy.nonzero(numpy.asarray(mask, dtype=bool))[0]
    if len(lanes) == 0:
        return
    if ref.array is not None:
        for k, item in enumerate(value.data if value.kind == "array" else []):
            ref_store(_ref_index(ref, numpy.full(n, k)), item, mask, n)
        return
    if ref.kind in ref.structs:
        for name, kind, count, offset in member_offsets(ref.structs[ref.kind], ref.structs, ref.std430):
            if isinstance(value.data, dict) and name in value.data:
                ref_store(Ref(ref.memory, ref.offset + offset, kind, count, ref.std430, ref.structs), value.data[name], mask, n)
        return
    if ref.kind in MATRICES:
        c, r = MATRICES[ref.kind]
        data = numpy.asarray(value.data).reshape(n, c, r)
        for k in range(c):
            ref_store(_ref_index(ref, numpy.full(n, k)), Val(vector_kind("float", r), data[:, k]), mask, n)
        return
    base = base_of(ref.kind)
    count = size_of(ref.kind)
    data = numpy.asarray(value.data)
    data = numpy.broadcast_to(data.reshape(n, -1) if data.ndim else numpy.full((n, 1), data), (n, count)) \
        if data.ndim <= 1 or data.shape[-1] in (1, count) else data.reshape(n, count)
    if base == "float":
        words = data.astype(numpy.float32).view(numpy.uint32)
    elif base == "bool":
        words = (data != 0).astype(numpy.uint32)
    else:
        words = (data.astype(numpy.int64) & 0xFFFFFFFF).astype(numpy.uint32)
    offset = numpy.broadcast_to(numpy.asarray(ref.offset, dtype=numpy.int64), (n,))[lanes]
    ok = (offset >= 0) & (offset + 4 * count <= len(ref.memory))
    lanes, offset = lanes[ok], offset[ok]
    raw = numpy.ascontiguousarray(words[lanes]).view(numpy.uint8).reshape(len(lanes), 4 * count)
    # the lanes in order: when two write the same place, the last one stays
    ref.memory[offset[:, None] + numpy.arange(4 * count)] = raw


ATOMICS = {"atomicAdd", "atomicMin", "atomicMax", "atomicAnd", "atomicOr", "atomicXor", "atomicExchange", "atomicCompSwap",
           "atomicCounterIncrement", "atomicCounterDecrement", "atomicCounter", "atomicCounterAdd", "atomicCounterSubtract"}


def ref_atomic(rt, name: str, ref: Ref, operands, mask) -> Val:
    """An atomic operation of the active lanes, one after the other in lane order: the old values."""
    n = rt.n
    kind = ref.kind
    signed = base_of(kind) == "int"
    old = ref_load(ref, n).data.astype(numpy.int64)
    out = old.copy()
    lanes = numpy.nonzero(numpy.asarray(mask, dtype=bool))[0]
    offsets = numpy.broadcast_to(numpy.asarray(ref.offset, dtype=numpy.int64), (n,))
    values = [numpy.broadcast_to(numpy.asarray(op.data).reshape(-1), (n,)).astype(numpy.int64) if op is not None else None
              for op in operands]
    memory = {}
    for lane in lanes:
        place = int(offsets[lane])
        if place < 0 or place + 4 > len(ref.memory):
            continue
        current = memory.get(place)
        if current is None:
            current = int.from_bytes(bytes(ref.memory[place:place + 4]), "little")
        value = current - (1 << 32) if signed and current & 0x80000000 else current
        out[lane] = value
        a = int(values[0][lane]) if values and values[0] is not None else 0
        if name in ("atomicAdd", "atomicCounterAdd"):
            new = value + a
        elif name == "atomicCounterSubtract":
            new = value - a
        elif name == "atomicCounterIncrement":
            new = value + 1
        elif name == "atomicCounterDecrement":
            new = value - 1
            out[lane] = new         # the decrement gives the new value
        elif name == "atomicMin":
            new = min(value, a)
        elif name == "atomicMax":
            new = max(value, a)
        elif name == "atomicAnd":
            new = value & a
        elif name == "atomicOr":
            new = value | a
        elif name == "atomicXor":
            new = value ^ a
        elif name == "atomicExchange":
            new = a
        elif name == "atomicCompSwap":
            new = int(values[1][lane]) if value == a else value
        else:   # atomicCounter: a read
            new = value
        current = new & 0xFFFFFFFF
        memory[place] = current
        ref.memory[place:place + 4] = numpy.frombuffer(current.to_bytes(4, "little"), dtype=numpy.uint8)
    return Val("int" if signed else "uint", out if base_of(kind) != "uint" else out & 0xFFFFFFFF)


# ------------------------------------------------------------------ images
IMAGES = {f"{p}image{d}" for p in ("", "i", "u") for d in ("1D", "2D", "3D", "Cube", "2DRect", "1DArray", "2DArray",
                                                         "CubeArray", "Buffer", "2DMS", "2DMSArray")}
SAMPLERS |= IMAGES | {"atomic_uint"}


class ImageUnit:
    """An image bound with glBindImageTexture: its texels (layers of (h, w, 4) float arrays) and format."""

    def __init__(self, layers, integer=False, unsigned=False, on_change=None):
        self.layers, self.integer, self.unsigned, self.on_change = layers, integer, unsigned, on_change


def _image_coords(rt, args, image):
    coords = numpy.asarray(args[1].data).reshape(rt.n, -1).astype(numpy.int64)
    first = image.layers[0] if image.layers else numpy.zeros((1, 1, 4))
    h, w = first.shape[:2]
    x = coords[:, 0]
    y = coords[:, 1] if coords.shape[1] > 1 else numpy.zeros(rt.n, dtype=numpy.int64)
    layer = coords[:, 2] if coords.shape[1] > 2 else numpy.zeros(rt.n, dtype=numpy.int64)
    inside = (x >= 0) & (x < w) & (y >= 0) & (y < h) & (layer >= 0) & (layer < len(image.layers))
    return numpy.clip(x, 0, w - 1), numpy.clip(y, 0, h - 1), numpy.clip(layer, 0, len(image.layers) - 1), inside


def image_call(rt, name, args, mask) -> Val:
    unit = int(numpy.asarray(args[0].data).reshape(-1)[0]) if isinstance(args[0].data, numpy.ndarray) else int(args[0].data)
    image = rt.images.get(unit)
    kind = args[0].kind
    base = "int" if kind.startswith("i") else "uint" if kind.startswith("u") else "float"
    if image is None or not image.layers:
        if name == "imageSize":
            return Val("ivec2", numpy.zeros((rt.n, 2), dtype=numpy.int64))
        return Val(vector_kind(base, 4), numpy.zeros((rt.n, 4), dtype=dtype_of(base)))
    if name == "imageSize":
        h, w = image.layers[0].shape[:2]
        values = [w, h] + ([len(image.layers)] if "Array" in kind or "3D" in kind else [])
        return Val(vector_kind("int", len(values)), numpy.tile(numpy.array(values, dtype=numpy.int64), (rt.n, 1)))
    x, y, layer, inside = _image_coords(rt, args, image)
    if name == "imageLoad":
        out = numpy.zeros((rt.n, 4))
        for k in numpy.unique(layer):
            sel = layer == k
            out[sel] = image.layers[int(k)][y[sel], x[sel]]
        out[~inside] = 0
        data = out if base == "float" else numpy.round(out).astype(numpy.int64)
        return Val(vector_kind(base, 4), data)
    if name == "imageStore":
        value = numpy.asarray(args[2].data, dtype=numpy.float64).reshape(rt.n, -1)
        if value.shape[1] < 4:
            value = numpy.concatenate([value, numpy.zeros((rt.n, 4 - value.shape[1]))], 1)
        lanes = numpy.nonzero(numpy.asarray(mask, dtype=bool) & inside)[0]
        for lane in lanes if len(numpy.unique(layer[lanes])) > 1 else []:
            image.layers[int(layer[lane])][y[lane], x[lane]] = value[lane]
        if len(lanes) and len(numpy.unique(layer[lanes])) <= 1:
            image.layers[int(layer[lanes[0]])][y[lanes], x[lanes]] = value[lanes]
        if image.on_change:
            image.on_change()
        return Val("void", None)
    # imageAtomic*: on the first channel, lane after lane
    lanes = numpy.nonzero(numpy.asarray(mask, dtype=bool) & inside)[0]
    operand = numpy.broadcast_to(numpy.asarray(args[2].data).reshape(-1), (rt.n,)).astype(numpy.int64)
    other = numpy.broadcast_to(numpy.asarray(args[3].data).reshape(-1), (rt.n,)).astype(numpy.int64) if len(args) > 3 else None
    out = numpy.zeros(rt.n, dtype=numpy.int64)
    for lane in lanes:
        texels = image.layers[int(layer[lane])]
        current = int(texels[y[lane], x[lane], 0])
        out[lane] = current
        a = int(operand[lane])
        new = {"imageAtomicAdd": current + a, "imageAtomicMin": min(current, a), "imageAtomicMax": max(current, a),
               "imageAtomicAnd": current & a, "imageAtomicOr": current | a, "imageAtomicXor": current ^ a,
               "imageAtomicExchange": a}.get(name, int(other[lane]) if other is not None and current == a else current)
        texels[y[lane], x[lane], 0] = new
    if image.on_change:
        image.on_change()
    return Val(base, out)


# ------------------------------------------------------------------ the stages besides vertex and fragment
def program_layout(program, qualifier: str) -> Dict[str, object]:
    """The layout of the declarations "layout(...) in;" / "layout(...) out;": name -> value (None for a
    flag such as triangles)."""
    out = {}
    for decl in program.layout_decls:
        if qualifier in decl["qualifiers"]:
            for name, value in decl["layout"].items():
                out[name] = _const_count(value) if value is not None else None
    return out


STAGE_OUTPUTS = {"gl_Position": "vec4", "gl_PointSize": "float", "gl_Layer": "int", "gl_ViewportIndex": "int",
                 "gl_PrimitiveID": "int"}


def _prepare(rt: "Runtime", program, uniforms, inputs, outputs=True, buffers=None, images=None, groups=None):
    for name, value in uniforms.items():
        rt.scopes[0][name] = value
    for name, value in inputs.items():
        rt.scopes[0][name] = value
    rt.images = images or {}
    n = rt.n
    if outputs:
        for name, kind in STAGE_OUTPUTS.items():
            rt.scopes[0].setdefault(name, zero(kind, n))
        rt.scopes[0].setdefault("gl_ClipDistance", zero("float", n, None, 8))
        for item in program.interface("out"):
            name, kind, array, layout, quals = item
            if name not in rt.scopes[0]:
                size = rt.array_size(array)
                rt.scopes[0][name] = zero(kind, n, program.structs, size if size and size > 0 else None)
        for block in program.blocks:
            if "out" in block["qualifiers"] and block["instance"] and block["instance"] not in rt.scopes[0]:
                fields = {name: zero(kind, n, program.structs) for name, kind, array, layout in block["members"]}
                rt.scopes[0][block["instance"]] = Val("__block", fields)
    bind_memory(rt, program, buffers or {}, groups)


def bind_memory(rt, program, buffers, groups=None):
    """The buffer blocks (buffers: binding -> numpy uint8), the atomic counters (binding -> bytes, as
    ("atomic", binding)) and the shared variables (one copy per work group: groups (n,) the group of each
    lane) as variables in memory."""
    n = rt.n
    zero_offset = numpy.zeros(n, dtype=numpy.int64)
    for block in program.blocks:
        if "buffer" not in block["qualifiers"]:
            continue
        binding = _const_count(block["layout"].get("binding")) or 0
        memory = buffers.get(binding)
        if memory is None:
            memory = numpy.zeros(0, dtype=numpy.uint8)
        std430 = "std140" not in block["layout"]
        members = [(name, kind, array, layout) for name, kind, array, layout in block["members"]]
        name = f"__buffer_{block['name']}"
        rt.structs = dict(rt.structs)
        rt.structs[name] = [(m[0], m[1], m[2]) for m in members]
        whole = Ref(memory, zero_offset, name, None, std430, rt.structs)
        if block["instance"]:
            rt.scopes[0][block["instance"]] = Val("__ref", whole)
        else:
            for member in members:
                rt.scopes[0][member[0]] = Val("__ref", _ref_field(whole, member[0]))
    for item in program.globals:
        v = item.value
        quals = set(v["qualifiers"])
        if v["kind"] == "atomic_uint" and "uniform" in quals:
            binding = _const_count(v["layout"].get("binding")) or 0
            memory = buffers.get(("atomic", binding))
            if memory is None:
                memory = numpy.zeros(4, dtype=numpy.uint8)
            base = _const_count(v["layout"].get("offset")) or 0
            for k, (name, array, init) in enumerate(v["items"]):
                count = _const_count(array)
                rt.scopes[0][name] = Val("__ref", Ref(memory, zero_offset + base + 4 * k, "uint",
                                                     count if count and count > 0 else None, True, rt.structs))
        elif "shared" in quals:
            group_of = numpy.zeros(n, dtype=numpy.int64) if groups is None else numpy.asarray(groups, dtype=numpy.int64)
            count_groups = int(group_of.max()) + 1 if n else 1
            for name, array, init in v["items"]:
                count = rt.array_size(array)
                size, _ = layout_of(v["kind"], rt.structs, True, count if count and count > 0 else None)
                memory = numpy.zeros(max(4, size) * count_groups, dtype=numpy.uint8)
                rt.scopes[0][name] = Val("__ref", Ref(memory, group_of * max(4, size), v["kind"],
                                                     count if count and count > 0 else None, True, rt.structs))


def run_compute(program, groups, uniforms, textures, buffers, images, chunk=1 << 18):
    """A dispatch of groups (x, y, z) work groups; the lanes of whole groups run together, chunk at most."""
    layout = program_layout(program, "in")
    local = (layout.get("local_size_x") or 1, layout.get("local_size_y") or 1, layout.get("local_size_z") or 1)
    per_group = local[0] * local[1] * local[2]
    total_groups = groups[0] * groups[1] * groups[2]
    step = max(1, chunk // max(per_group, 1))
    for first in range(0, total_groups, step):
        count = min(step, total_groups - first)
        n = count * per_group
        lane = numpy.arange(n, dtype=numpy.int64)
        group = first + lane // per_group
        index = lane % per_group
        local_id = numpy.stack([index % local[0], (index // local[0]) % local[1], index // (local[0] * local[1])], 1)
        group_id = numpy.stack([group % groups[0], (group // groups[0]) % groups[1], group // (groups[0] * groups[1])], 1)
        rt = Runtime(program, n, textures)
        inputs = {"gl_LocalInvocationID": Val("uvec3", local_id), "gl_WorkGroupID": Val("uvec3", group_id),
                  "gl_GlobalInvocationID": Val("uvec3", group_id * numpy.array(local) + local_id),
                  "gl_NumWorkGroups": Val("uvec3", numpy.tile(numpy.array(groups, dtype=numpy.int64), (n, 1))),
                  "gl_WorkGroupSize": Val("uvec3", numpy.tile(numpy.array(local, dtype=numpy.int64), (n, 1))),
                  "gl_LocalInvocationIndex": Val("uint", index)}
        _prepare(rt, program, {k: _subset(v, n) for k, v in uniforms.items()}, inputs, False, buffers, images, group - first)
        rt.run_main()


def _subset(value: Val, n: int) -> Val:
    """A uniform given for some lanes, for n lanes (its first row repeated)."""
    if value.kind == "array":
        return Val("array", [_subset(v, n) for v in value.data], value.length)
    if isinstance(value.data, dict):
        return Val(value.kind, {k: _subset(v, n) for k, v in value.data.items()})
    if isinstance(value.data, numpy.ndarray) and value.data.ndim >= 1 and value.data.shape[0] != n and value.data.shape[0] >= 1:
        return Val(value.kind, numpy.repeat(value.data[:1], n, axis=0))
    return value


def run_geometry(program, n, inputs, uniforms, textures, buffers=None, images=None):
    """The geometry shader on n primitives: inputs are arrays (one item per vertex of the primitive). Returns
    the runs (one per invocation), each with its events (emit / cut, stream, lanes, outputs)."""
    layout = program_layout(program, "in")
    runs = []
    for invocation in range(layout.get("invocations") or 1):
        rt = Runtime(program, n, textures)
        values = dict(inputs)
        values["gl_InvocationID"] = Val("int", numpy.full(n, invocation, dtype=numpy.int64))
        _prepare(rt, program, uniforms, values, True, buffers, images)
        rt.events = []
        rt.run_main()
        runs.append(rt.events)
    return runs


def run_stage(program, n, inputs, uniforms, textures, buffers=None, images=None) -> "Runtime":
    """A tessellation control / evaluation shader (or any stage with outputs) on n lanes."""
    rt = Runtime(program, n, textures)
    _prepare(rt, program, uniforms, inputs, True, buffers, images)
    rt.scopes[0].setdefault("gl_TessLevelOuter", zero("float", n, None, 4))
    rt.scopes[0].setdefault("gl_TessLevelInner", zero("float", n, None, 2))
    rt.run_main()
    return rt


def emit_outputs(rt) -> Dict[str, Val]:
    """The values of the outputs of a geometry shader (at EmitVertex)."""
    out = {}
    for name in list(STAGE_OUTPUTS) + [item[0] for item in rt.p.interface("out")]:
        if rt.has(name):
            out[name] = rt.lookup(name).copy()
    for block in rt.p.blocks:
        if "out" in block["qualifiers"] and block["instance"] and rt.has(block["instance"]):
            out[block["instance"]] = rt.lookup(block["instance"]).copy()
    return out


def _rows(value: Val, lanes) -> Val:
    """A value restricted to some lanes."""
    if value.kind == "array":
        return Val("array", [_rows(v, lanes) for v in value.data], value.length)
    if isinstance(value.data, dict):
        return Val(value.kind, {k: _rows(v, lanes) for k, v in value.data.items()})
    if isinstance(value.data, numpy.ndarray) and value.data.ndim >= 1:
        return Val(value.kind, value.data[lanes].copy())
    return value
