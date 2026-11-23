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
# derivatives (dFdx, dFdy, fwidth) give 0. Not covered: geometry, tessellation and compute shaders,
# subroutines, images and atomic counters.

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
            return []
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
        for item in self.items:
            if item.op == "function":
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
                if self.has(name) and ("uniform" in quals or "in" in quals or "attribute" in quals or
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
    def store(self, node: Node, value: Val, mask):
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
            return self.lookup(node.value)
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
            base = self.eval(node.args[0], mask)
            if isinstance(base.data, dict):
                return base.data[node.value]
            indices = _swizzle(node.value)
            data = base.data if base.data.ndim > 1 else base.data[:, None]
            picked = data[:, indices]
            kind = vector_kind(base_of(base.kind), len(indices))
            return Val(kind, picked[:, 0] if len(indices) == 1 else picked)
        if op == "index":
            base = self.eval(node.args[0], mask)
            index = numpy.asarray(self.eval(node.args[1], mask).data).reshape(-1)
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
            base = self.eval(node.args[0], mask)
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
        if "Offset" in name and len(args) > 2:
            offset = tuple(int(v) for v in as_columns(args[-1], self.n)[0][:2])
        if name.startswith("texelFetch"):
            lod = as_columns(args[2], self.n)[:, 0] if len(args) > 2 else None
            result = function(sampler.kind, coords, "fetch", lod) if function is not None else numpy.zeros((self.n, 4))
        else:
            result = function(sampler.kind, coords, offset, lod) if function is not None else numpy.zeros((self.n, 4))
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
    "dFdx": _zero_like, "dFdy": _zero_like, "fwidth": _zero_like, "dFdxFine": _zero_like, "dFdyFine": _zero_like,
    "dFdxCoarse": _zero_like, "dFdyCoarse": _zero_like, "fwidthFine": _zero_like, "fwidthCoarse": _zero_like,
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
