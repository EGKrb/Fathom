# Fathom addition to dumpulator: a reader of the LLVM 3.7 bitcode of DXIL (the shaders of shader model 6,
# compiled by dxc): the module's types, global variables, functions, constants, metadata and the
# instructions of the functions, as Python objects for dxil.py. Boost Software License 1.0, like the rest
# of dumpulator (LICENSE.dumpulator).
#
# The bitstream (abbreviations, blocks, the BLOCKINFO block) and the records follow LLVM's
# BitcodeReader of version 3.7, the one DXIL is: relative value numbers in the functions, explicit
# types, the old metadata records.

import struct
from typing import Dict, List, Optional


class BitcodeError(Exception):
    pass


class Bits:
    def __init__(self, data: bytes):
        self.data = data
        self.value = int.from_bytes(data, "little")
        self.position = 0
        self.length = len(data) * 8

    def read(self, width: int) -> int:
        if width == 0:
            return 0
        if self.position + width > self.length:
            raise BitcodeError("past the end of the bitcode")
        value = (self.value >> self.position) & ((1 << width) - 1)
        self.position += width
        return value

    def vbr(self, width: int) -> int:
        result, shift = 0, 0
        while True:
            piece = self.read(width)
            result |= (piece & ((1 << (width - 1)) - 1)) << shift
            shift += width - 1
            if not piece >> (width - 1):
                return result

    def align32(self):
        self.position = (self.position + 31) // 32 * 32


class Block:
    def __init__(self, block_id):
        self.id = block_id
        self.records: List[tuple] = []        # (code, operands, blob)
        self.blocks: List["Block"] = []
        self.items: List[object] = []         # the records and sub-blocks in order


def _read_abbrev(bits: Bits):
    count = bits.vbr(5)
    ops = []
    while len(ops) < count:
        literal = bits.read(1)
        if literal:
            ops.append(("literal", bits.vbr(8)))
            continue
        encoding = bits.read(3)
        if encoding == 1:
            ops.append(("fixed", bits.vbr(5)))
        elif encoding == 2:
            ops.append(("vbr", bits.vbr(5)))
        elif encoding == 3:
            ops.append(("array", None))
        elif encoding == 4:
            ops.append(("char6", None))
        elif encoding == 5:
            ops.append(("blob", None))
        else:
            raise BitcodeError(f"abbreviation encoding {encoding}")
    return ops


def _scalar(bits: Bits, op):
    kind, width = op
    if kind == "literal":
        return width
    if kind == "fixed":
        return bits.read(width)
    if kind == "vbr":
        return bits.vbr(width)
    if kind == "char6":
        value = bits.read(6)
        return "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._"[value].encode()[0]
    raise BitcodeError(f"operand {kind}")


def _read_record(bits: Bits, abbrev):
    values = []
    blob = None
    index = 0
    while index < len(abbrev):
        op = abbrev[index]
        if op[0] == "array":
            element = abbrev[index + 1]
            count = bits.vbr(6)
            values.extend(_scalar(bits, element) for _ in range(count))
            index += 2
            continue
        if op[0] == "blob":
            size = bits.vbr(6)
            bits.align32()
            start = bits.position // 8
            blob = bits.data[start:start + size]
            bits.position += size * 8
            bits.align32()
            index += 1
            continue
        values.append(_scalar(bits, op))
        index += 1
    return values[0], values[1:], blob


def read_block(bits: Bits, block_id: int, width: int, blockinfo: Dict[int, list]) -> Block:
    block = Block(block_id)
    abbrevs = list(blockinfo.get(block_id, []))
    current_info = None
    while True:
        code = bits.read(width)
        if code == 0:       # END_BLOCK
            bits.align32()
            return block
        if code == 1:       # ENTER_SUBBLOCK
            sub_id = bits.vbr(8)
            sub_width = bits.vbr(4)
            bits.align32()
            bits.read(32)   # the length in words
            sub = read_block(bits, sub_id, sub_width, blockinfo)
            block.blocks.append(sub)
            block.items.append(sub)
            continue
        if code == 2:       # DEFINE_ABBREV
            abbrev = _read_abbrev(bits)
            if block_id == 0:
                blockinfo.setdefault(current_info, []).append(abbrev)
            else:
                abbrevs.append(abbrev)
            continue
        if code == 3:       # UNABBREV_RECORD
            record_code = bits.vbr(6)
            count = bits.vbr(6)
            operands = [bits.vbr(6) for _ in range(count)]
            record = (record_code, operands, None)
        else:
            record = _read_record(bits, abbrevs[code - 4])
        if block_id == 0 and record[0] == 1:    # BLOCKINFO SETBID
            current_info = record[1][0]
            continue
        block.records.append(record)
        block.items.append(record)


def parse_bitstream(data: bytes) -> Block:
    if data[:4] != b"BC\xc0\xde":
        raise BitcodeError("not LLVM bitcode")
    bits = Bits(data)
    bits.position = 32
    top = Block(-1)
    blockinfo: Dict[int, list] = {}
    while bits.position + 32 <= bits.length:
        code = bits.read(2)
        if code != 1:
            break
        block_id = bits.vbr(8)
        width = bits.vbr(4)
        bits.align32()
        bits.read(32)
        block = read_block(bits, block_id, width, blockinfo)
        top.blocks.append(block)
    return top


# ------------------------------------------------------------------ the module
class Type:
    __slots__ = ("kind", "bits", "elements", "count", "pointee", "space", "ret", "params", "name")

    def __init__(self, kind, bits=0, elements=None, count=0, pointee=None, space=0, ret=None, params=None, name=None):
        self.kind, self.bits, self.elements, self.count = kind, bits, elements or [], count
        self.pointee, self.space, self.ret, self.params, self.name = pointee, space, ret, params or [], name

    def __repr__(self):
        if self.kind == "int":
            return f"i{self.bits}"
        if self.kind == "ptr":
            return f"{self.pointee}*"
        if self.kind == "struct":
            return f"%{self.name or 'anon'}"
        if self.kind in ("array", "vector"):
            return f"[{self.count} x {self.elements[0]}]"
        return self.kind


class Value:
    """A value of the module: kind "global", "function", "const", "arg", "inst"."""
    __slots__ = ("kind", "type", "value", "name", "id")

    def __init__(self, kind, type_, value=None, name=None):
        self.kind, self.type, self.value, self.name = kind, type_, value, name
        self.id = -1

    def __repr__(self):
        return f"Value({self.kind}, {self.type}, {self.value if self.kind == 'const' else self.name})"


class Instruction:
    __slots__ = ("op", "type", "operands", "extra", "id", "block")

    def __init__(self, op, type_, operands, extra=None):
        self.op, self.type, self.operands, self.extra = op, type_, operands, extra or {}
        self.id = None          # the value number (None: no value)
        self.block = 0

    def __repr__(self):
        return f"%{self.id} = {self.op} {self.operands} {self.extra}"


class Function:
    def __init__(self, name, type_, declaration):
        self.name, self.type, self.declaration = name, type_, declaration
        self.blocks: List[List[Instruction]] = []
        self.args: List[Value] = []
        self.values: Dict[int, Value] = {}       # function-level constants and arguments by value number
        self.value_id = -1


BINOPS = ["add", "sub", "mul", "udiv", "sdiv", "urem", "srem", "shl", "lshr", "ashr", "and", "or", "xor"]
CASTS = ["trunc", "zext", "sext", "fptoui", "fptosi", "uitofp", "sitofp", "fptrunc", "fpext", "ptrtoint", "inttoptr",
         "bitcast", "addrspacecast"]
RMW = ["xchg", "add", "sub", "and", "nand", "or", "xor", "max", "min", "umax", "umin"]


def _signed(v):
    return -(v >> 1) if v & 1 else v >> 1


def _text(values):
    return bytes(v & 0xFF for v in values).decode("latin-1")


class Module:
    """A DXIL module: types, values (globals, functions, constants), functions with their instructions,
    metadata (as Python lists / values / strings) and named metadata."""

    def __init__(self, bitcode: bytes):
        top = parse_bitstream(bitcode)
        module = next((b for b in top.blocks if b.id == 8), None)
        if module is None:
            raise BitcodeError("no module block")
        self.types: List[Type] = []
        self.values: List[Value] = []
        self.functions: List[Function] = []
        self.metadata: List[object] = []
        self.named: Dict[str, list] = {}
        self.struct_name = None
        self._metadata_block = None
        bodies = []
        for item in module.items:
            if isinstance(item, Block):
                if item.id == 17:
                    self._types(item)
                elif item.id == 11:
                    self._constants(item, self.values)
                elif item.id == 15 and any(r[0] != 6 for r in item.records):
                    self._metadata_block = item
                elif item.id == 14:
                    self._symbols(item, self.values)
                elif item.id == 12:
                    bodies.append(item)
                continue
            code, ops, _ = item
            if code == 7:       # GLOBALVAR: [type, isconst | explicit << 1, initid, linkage, alignment, section, ...]
                explicit = (ops[1] >> 1) & 1
                type_ = self.types[ops[0]]
                space = (ops[1] >> 2) if explicit else 0
                pointer = Type("ptr", pointee=type_, space=space) if explicit else type_
                value = Value("global", pointer, dict(init=ops[2] - 1 if ops[2] else None, constant=ops[1] & 1))
                self._add(value)
            elif code == 8:     # FUNCTION: [type, cc, isproto, linkage, ...]
                type_ = self.types[ops[0]]
                ftype = type_.pointee if type_.kind == "ptr" else type_
                function = Function("", ftype, bool(ops[2]))
                value = Value("function", Type("ptr", pointee=ftype), function)
                function.value_id = self._add(value)
                self.functions.append(function)
        if self._metadata_block is not None:
            self._read_metadata(self._metadata_block)
        definitions = [f for f in self.functions if not f.declaration]
        for function, body in zip(definitions, bodies):
            self._function(function, body)

    def _add(self, value: Value) -> int:
        value.id = len(self.values)
        self.values.append(value)
        return value.id

    # types -------------------------------------------------------------------
    def _types(self, block):
        name = None
        for code, ops, _ in block.records:
            if code == 1:
                continue
            if code == 2:
                t = Type("void")
            elif code == 3:
                t = Type("float", 32)
            elif code == 4:
                t = Type("double", 64)
            elif code == 10:
                t = Type("half", 16)
            elif code == 5:
                t = Type("label")
            elif code == 16:
                t = Type("metadata")
            elif code == 7:
                t = Type("int", ops[0])
            elif code == 8:
                t = Type("ptr", pointee=None, space=ops[1] if len(ops) > 1 else 0)
                t.elements = [ops[0]]
            elif code == 11:
                t = Type("array", count=ops[0])
                t.elements = [ops[1]]
            elif code == 12:
                t = Type("vector", count=ops[0])
                t.elements = [ops[1]]
            elif code == 19:
                name = _text(ops)
                continue
            elif code in (18, 20):
                t = Type("struct", name=name if code == 20 else None)
                t.elements = list(ops[1:])
                name = None
            elif code == 6:
                t = Type("struct", name=name)
                name = None
            elif code == 21:
                t = Type("function")
                t.ret, t.params = ops[1], list(ops[2:])
            else:
                t = Type(f"unknown{code}")
            self.types.append(t)
        # the references by index, resolved
        for t in self.types:
            if t.kind == "ptr":
                t.pointee = self.types[t.elements[0]]
                t.elements = []
            elif t.kind in ("array", "vector", "struct"):
                t.elements = [self.types[i] for i in t.elements]
            elif t.kind == "function":
                t.ret = self.types[t.ret]
                t.params = [self.types[i] for i in t.params]

    # constants ---------------------------------------------------------------
    def _constants(self, block, into: list, function=None):
        current = None
        for code, ops, blob in block.records:
            if code == 1:
                current = self.types[ops[0]]
                continue
            value = Value("const", current)
            if code == 2:
                value.value = ("null",)
            elif code == 3:
                value.value = ("undef",)
            elif code == 4:
                value.value = _signed(ops[0])
            elif code == 6:
                bits = ops[0]
                if current.kind == "double":
                    value.value = struct.unpack("<d", struct.pack("<Q", bits & (2 ** 64 - 1)))[0]
                elif current.kind == "half":
                    value.value = float(struct.unpack("<e", struct.pack("<H", bits & 0xFFFF))[0])
                else:
                    value.value = struct.unpack("<f", struct.pack("<I", bits & 0xFFFFFFFF))[0]
            elif code == 7:
                value.value = ("aggregate", list(ops))
            elif code in (8, 9):
                value.value = ("string", _text(ops))
            elif code == 22:
                value.value = ("data", list(ops))
            elif code == 11:        # CE_CAST: [opcode, opty, opval]
                value.value = ("cast", CASTS[ops[0]], ops[2])
            elif code in (12, 20):  # CE_GEP / CE_INBOUNDS_GEP: [pointee type?] (type, value) pairs
                items = ops[1:] if len(ops) % 2 else ops
                value.value = ("gep", [items[k + 1] for k in range(0, len(items), 2)])
            elif code == 10:        # CE_BINOP
                value.value = ("binop", BINOPS[ops[0]], ops[1], ops[2])
            else:
                value.value = ("unsupported", code)
            if function is None:
                self._add(value)
            else:
                value.id = len(into)
                into.append(value)

    # names -------------------------------------------------------------------
    def _symbols(self, block, values):
        for code, ops, _ in block.records:
            if code == 1 and ops[0] < len(values):      # VST_ENTRY: [valueid, name]
                target = values[ops[0]]
                name = _text(ops[1:])
                target.name = name
                if target.kind == "function":
                    target.value.name = name

    # metadata ----------------------------------------------------------------
    def _read_metadata(self, block):
        pending_name = None
        for code, ops, _ in block.records:
            if code == 4:           # NAME
                pending_name = _text(ops)
                continue
            if code == 10:          # NAMED_NODE
                self.named[pending_name] = [self.metadata[i] if i < len(self.metadata) else None for i in ops]
                pending_name = None
                continue
            if code == 1:           # STRING
                self.metadata.append(_text(ops))
            elif code == 2:         # VALUE: [type, value]
                self.metadata.append(("value", ops[1]))
            elif code in (3, 5):    # NODE / DISTINCT_NODE: [n x (md + 1)]
                self.metadata.append(("node", [o - 1 if o else None for o in ops]))
            elif code in (6, 11):   # KIND / ATTACHMENT: no metadata number
                continue
            else:
                self.metadata.append(("other", code))
        # the nodes' operands resolved (they may refer forward)
        resolved = []
        for item in self.metadata:
            resolved.append(item)
        self._raw_metadata = resolved
        self.metadata = [self._resolve(i, set()) for i in range(len(resolved))]
        self.named = {k: [self._resolve_item(x, set()) for x in v] for k, v in self.named.items()}

    def _resolve_item(self, item, seen):
        return item if not isinstance(item, tuple) or item[0] != "node" else self._node(item, seen)

    def _resolve(self, index, seen):
        if index is None or index >= len(self._raw_metadata):
            return None
        item = self._raw_metadata[index]
        if isinstance(item, tuple) and item[0] == "node":
            if index in seen:
                return None
            return self._node(item, seen | {index})
        if isinstance(item, tuple) and item[0] == "value":
            return self.values[item[1]] if item[1] < len(self.values) else None
        return item

    def _node(self, item, seen):
        return [self._resolve(i, seen) for i in item[1]]

    def constant(self, value):
        """The Python value of a constant (or of a metadata value)."""
        if isinstance(value, Value) and value.kind == "const":
            return value.value
        return value

    # functions ---------------------------------------------------------------
    def _function(self, function: Function, block: Block):
        values: List[Optional[Value]] = list(self.values)
        for param in function.type.params:
            arg = Value("arg", param)
            arg.id = len(values)
            values.append(arg)
            function.args.append(arg)
        for item in block.items:
            if isinstance(item, Block):
                if item.id == 11:
                    self._constants(item, values, function)
                elif item.id == 14:
                    self._symbols(item, values)
        for value in values[len(self.values):]:
            if value is not None and value.kind in ("arg", "const"):
                function.values[value.id] = value
        next_id = len(values)
        blocks: List[List[Instruction]] = [[]]
        types = self.types

        def absolute(relative):
            return next_id - relative

        for record in block.records:
            code, ops, _ = record
            ins = None
            if code == 1:           # DECLAREBLOCKS
                blocks = [[] for _ in range(ops[0])]
                current = 0
                continue
            i = 0

            def operand():
                nonlocal i
                value = absolute(ops[i])
                i += 1
                if value >= next_id:    # a forward reference: its type follows
                    i += 1
                return value

            if code == 2:           # BINOP
                a = operand()
                b = operand()
                ins = Instruction("binop", None, [a, b], dict(op=BINOPS[ops[i]] if not self._float_of(values, a) else
                                                              {"add": "fadd", "sub": "fsub", "mul": "fmul", "udiv": "fdiv",
                                                               "sdiv": "fdiv", "urem": "frem", "srem": "frem"}[BINOPS[ops[i]]]))
                ins.type = self._type_of(values, a)
            elif code == 3:         # CAST
                a = operand()
                ins = Instruction("cast", types[ops[i]], [a], dict(op=CASTS[ops[i + 1]]))
            elif code == 5 or code == 29:   # SELECT / VSELECT: [true, false, cond]
                a = operand()
                b = operand()
                c = operand()
                ins = Instruction("select", self._type_of(values, a), [c, a, b])
            elif code == 6:         # EXTRACTELT
                a = operand()
                b = operand()
                ins = Instruction("extractelement", self._type_of(values, a).elements[0], [a, b])
            elif code == 7:         # INSERTELT
                a = operand()
                b = operand()
                c = operand()
                ins = Instruction("insertelement", self._type_of(values, a), [a, b, c])
            elif code in (9, 28):   # CMP / CMP2: [lhs, rhs, predicate]
                a = operand()
                b = operand()
                ins = Instruction("cmp", Type("int", 1), [a, b], dict(predicate=ops[i]))
            elif code == 10:        # RET
                ins = Instruction("ret", None, [operand()] if ops else [])
            elif code == 11:        # BR: [true bb, false bb, cond] / [bb]
                if len(ops) == 1:
                    ins = Instruction("br", None, [], dict(targets=[ops[0]]))
                else:
                    cond = absolute(ops[2])
                    ins = Instruction("br", None, [cond], dict(targets=[ops[0], ops[1]]))
            elif code == 12:        # SWITCH: [opty, cond, default, (value, bb)...]
                cond = absolute(ops[1])
                cases = [(ops[k], ops[k + 1]) for k in range(3, len(ops), 2)]
                ins = Instruction("switch", None, [cond], dict(default=ops[2], cases=cases))
            elif code == 15:        # UNREACHABLE
                ins = Instruction("unreachable", None, [])
            elif code == 16:        # PHI: [type, (value signed relative, bb)...]
                incoming = []
                for k in range(1, len(ops) - 1, 2):
                    incoming.append((next_id - _signed(ops[k]), ops[k + 1]))
                ins = Instruction("phi", types[ops[0]], [v for v, _ in incoming], dict(blocks=[b for _, b in incoming]))
            elif code == 19:        # ALLOCA: [instty, opty, op, align]
                ins = Instruction("alloca", Type("ptr", pointee=types[ops[0]]), [], dict(size=ops[2]))
            elif code == 20:        # LOAD: [op, ty, align, vol]
                a = operand()
                ins = Instruction("load", types[ops[i]], [a])
            elif code == 26:        # EXTRACTVAL: [val, indices...]
                a = operand()
                indices = ops[i:]
                t = self._type_of(values, a)
                for index in indices:
                    t = t.elements[index] if t.kind == "struct" else t.elements[0]
                ins = Instruction("extractvalue", t, [a], dict(indices=list(indices)))
            elif code == 27:        # INSERTVAL: [val, val, indices...]
                a = operand()
                b = operand()
                ins = Instruction("insertvalue", self._type_of(values, a), [a, b], dict(indices=list(ops[i:])))
            elif code == 34:        # CALL: [paramattrs, cc, (fmf), (fnty), fnid, args...]
                cc = ops[1]
                i = 2
                if cc >> 17 & 1:
                    i += 1          # fast math flags
                ftype = None
                if cc >> 15 & 1:
                    ftype = types[ops[i]]
                    i += 1
                callee = operand()
                target = values[callee]
                if ftype is None:
                    ftype = target.type.pointee if target.type.kind == "ptr" else target.type
                args = []
                for _ in ftype.params:
                    args.append(operand())
                ins = Instruction("call", ftype.ret, args, dict(callee=callee))
            elif code == 43:        # GEP: [inbounds, ty, (value)...]
                i = 2
                pointer = operand()
                indices = []
                while i < len(ops):
                    indices.append(operand())
                ins = Instruction("gep", None, [pointer] + indices, dict(source=types[ops[1]]))
                ins.type = self._gep_type(values, pointer, types[ops[1]], indices)
            elif code == 44:        # STORE: [ptr, val, align, vol]
                p = operand()
                v = operand()
                ins = Instruction("store", None, [p, v])
            elif code == 38:        # ATOMICRMW: [ptr, val, op, vol, ordering, scope]
                p = operand()
                v = absolute(ops[i])
                i += 1
                ins = Instruction("atomicrmw", self._type_of(values, v), [p, v], dict(op=RMW[ops[i]]))
            elif code == 46:        # CMPXCHG: [ptr, cmp, new, vol, success ordering, scope, failure ordering, weak]
                p = operand()
                c = operand()
                n = absolute(ops[i])
                t = self._type_of(values, c)
                ins = Instruction("cmpxchg", Type("struct", elements=[t, Type("int", 1)]), [p, c, n])
            elif code in (33, 35, 36):  # debug locations, fence
                continue
            else:
                raise BitcodeError(f"instruction record {code}")
            ins.block = current
            blocks[current].append(ins)
            if ins.type is not None and ins.type.kind != "void" and ins.op not in ("store", "br", "ret", "switch", "unreachable"):
                ins.id = next_id
                value = Value("inst", ins.type, ins)
                value.id = next_id
                values.append(value)
                next_id += 1
            if ins.op in ("br", "ret", "switch", "unreachable"):
                current += 1
        function.blocks = blocks
        function.all_values = values

    def _type_of(self, values, index) -> Type:
        return values[index].type if index < len(values) and values[index] is not None else Type("int", 32)

    def _float_of(self, values, index) -> bool:
        t = self._type_of(values, index)
        while t.kind == "vector":
            t = t.elements[0]
        return t.kind in ("float", "double", "half")

    def _gep_type(self, values, pointer, source: Type, indices) -> Type:
        t = source
        space = self._type_of(values, pointer).space if self._type_of(values, pointer).kind == "ptr" else 0
        for index in indices[1:]:
            if t.kind == "struct":
                constant = values[index].value if values[index].kind == "const" else 0
                t = t.elements[constant if isinstance(constant, int) else 0]   # a null constant: 0
            else:
                t = t.elements[0]
        return Type("ptr", pointee=t, space=space)


def program(container: bytes):
    """The bitcode of the DXIL part of a DXBC container (or of its program header): (shader kind, model,
    bitcode)."""
    kind_version, size = struct.unpack_from("<2I", container, 0)
    magic, version, offset, length = struct.unpack_from("<4sIII", container, 8)
    if magic != b"DXIL":
        raise BitcodeError("not a DXIL program")
    start = 8 + offset
    return kind_version >> 16, ((kind_version >> 4) & 0xF, kind_version & 0xF), container[start:start + length]
