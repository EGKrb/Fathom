"""miasm on the debuggee: the code of a function read from a minidump of the process (its memory as it is
now: unpacked, patched), disassembled and lifted by miasm (vendored in analysis/miasm).

    python fathom_miasm.py ir   DUMP ADDRESS [--arch x64|x86] [--result FILE]
    python fathom_miasm.py symb DUMP ADDRESS [--end ADDRESS] [--arch x64|x86] [--result FILE]

ir:   miasm's intermediate representation of the blocks of the function at ADDRESS.
symb: symbolic execution from ADDRESS (to END, or until a block cannot be followed concretely): the final
      registers and memory as expressions of their initial values, simplified by miasm - what the code
      computes, without its obfuscation.

Used by the miasm.* commands of Fathom's ExtraCmds plugin. Needs Python 3.10+ with future and pyparsing
(requirements.txt).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # miasm (vendored)
# the minidump reader of Fathom's emulator (emulator/minidump), next to this folder in a release
for candidate in (os.path.join(HERE, "..", "emulator"), os.environ.get("FATHOM_EMULATOR", "")):
    if candidate and os.path.isdir(os.path.join(candidate, "minidump")):
        sys.path.append(os.path.abspath(candidate))
        break

from minidump.minidumpfile import MinidumpFile  # noqa: E402

from miasm.analysis.machine import Machine  # noqa: E402
from miasm.core.bin_stream import bin_stream  # noqa: E402
from miasm.core.locationdb import LocationDB  # noqa: E402
from miasm.ir.symbexec import SymbolicExecutionEngine  # noqa: E402
from miasm.expression.expression import ExprInt, ExprLoc  # noqa: E402
from miasm.expression.simplifications import expr_simp  # noqa: E402


class bin_stream_dump(bin_stream):
    """The memory of the minidump, for miasm's disassembler (unreadable bytes read as zero)."""

    def __init__(self, reader, attrib):
        super().__init__()
        self.reader = reader
        self.attrib = attrib  # 64 or 32: the size of the addresses
        self.cache = {}

    def _page(self, page):
        if page not in self.cache:
            try:
                self.cache[page] = self.reader.read(page, 0x1000)
            except Exception:
                data = bytearray(0x1000)
                for offset in range(0, 0x1000, 0x10):  # part of the page may be readable
                    try:
                        data[offset:offset + 0x10] = self.reader.read(page + offset, 0x10)
                    except Exception:
                        pass
                self.cache[page] = bytes(data)
        return self.cache[page]

    def _getbytes(self, start, l=1):
        out = bytearray()
        address = start
        while len(out) < l:
            page = address & ~0xFFF
            data = self._page(page)
            chunk = data[address - page:address - page + (l - len(out))]
            out += chunk
            address += len(chunk)
        return bytes(out)

    def getlen(self):
        return 1 << self.attrib


def load(dump, arch):
    reader = MinidumpFile.parse(dump).get_reader()
    machine = Machine("x86_64" if arch == "x64" else "x86_32")
    stream = bin_stream_dump(reader, 64 if arch == "x64" else 32)
    loc_db = LocationDB()
    mdis = machine.dis_engine(stream, loc_db=loc_db, follow_call=False, dontdis_retcall=False)
    mdis.dont_dis_nulstart_bloc = True
    return machine, mdis, loc_db


def action_ir(args) -> dict:
    machine, mdis, loc_db = load(args.dump, args.arch)
    asmcfg = mdis.dis_multiblock(args.address)
    lifter = machine.lifter_model_call(loc_db)
    ircfg = lifter.new_ircfg_from_asmcfg(asmcfg)
    blocks = sorted(ircfg.blocks.values(), key=lambda b: loc_db.get_location_offset(b.loc_key) or 0)
    lines = 0
    for block in blocks:
        text = str(block)
        print(text)
        print()
        lines += text.count("\n") + 1
    print(f"{len(asmcfg.blocks)} blocks, {sum(len(b.lines) for b in asmcfg.blocks)} instructions")
    return dict(ok=True, blocks=len(ircfg.blocks), lines=lines)


def action_symb(args) -> dict:
    machine, mdis, loc_db = load(args.dump, args.arch)
    lifter = machine.lifter_model_call(loc_db)
    asmcfg = mdis.dis_multiblock(args.address)
    ircfg = lifter.new_ircfg_from_asmcfg(asmcfg)
    engine = SymbolicExecutionEngine(lifter)
    address = ExprInt(args.address, lifter.pc.size)
    steps = 0
    stopped = "the end of the function"
    while steps < args.max_blocks:
        if args.end is not None and isinstance(address, ExprInt) and int(address) == args.end:
            stopped = f"reached {args.end:#x}"
            break
        if isinstance(address, ExprInt):
            target = loc_db.get_offset_location(int(address))
            if target is None or target not in ircfg.blocks:
                # code outside the disassembled function: disassemble from there
                asmcfg = mdis.dis_multiblock(int(address))
                ircfg = lifter.new_ircfg_from_asmcfg(asmcfg)
                target = loc_db.get_offset_location(int(address))
                if target is None or target not in ircfg.blocks:
                    stopped = f"no code at {int(address):#x}"
                    break
        elif isinstance(address, ExprLoc):
            target = address.loc_key
            if target not in ircfg.blocks:
                stopped = "a location outside the function"
                break
        elif address.is_mem() and str(address.ptr) in ("RSP", "ESP"):
            stopped = "the return of the function"
            break
        else:
            stopped = f"a branch that depends on the inputs ({expr_simp(address)})"
            break
        address = engine.run_block_at(ircfg, target)
        steps += 1
    print(f"symbolic execution from {args.address:#x}: {steps} blocks, stopped at {stopped}")
    print("registers:")
    changed = 0
    for reg, value in sorted(engine.modified(ids=True, mems=False), key=lambda kv: str(kv[0])):
        print(f"  {reg} = {expr_simp(value)}")
        changed += 1
    print("memory:")
    for mem, value in engine.modified(ids=False, mems=True):
        print(f"  {mem} = {expr_simp(value)}")
        changed += 1
    return dict(ok=True, blocks=steps, changed=changed, stopped=stopped)


def number(text: str) -> int:
    return int(text, 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--result", help="write the result as JSON to this file")
    actions = parser.add_subparsers(dest="action", required=True)
    for name, handler in (("ir", action_ir), ("symb", action_symb)):
        sub = actions.add_parser(name)
        sub.add_argument("dump")
        sub.add_argument("address", type=number)
        sub.add_argument("--arch", choices=("x64", "x86"), default="x64")
        sub.add_argument("--result", default=argparse.SUPPRESS)
        if name == "symb":
            sub.add_argument("--end", type=number)
            sub.add_argument("--max-blocks", type=int, default=1000)
        sub.set_defaults(handler=handler)
    args = parser.parse_args()
    try:
        result = args.handler(args)
    except Exception as error:  # reported to the plugin
        import traceback
        traceback.print_exc()
        result = dict(ok=False, error=f"{type(error).__name__}: {error}")
    if args.result:
        with open(args.result, "w", encoding="utf-8") as f:
            json.dump(result, f)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
