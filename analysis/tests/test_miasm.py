"""Test of fathom_miasm.py without a real minidump: the minidump reader is replaced by one that serves the code of
(x ^ y) + 2 * (x & y) at a fixed address, then `ir` and `symb` run on it, for x64 (arguments in rcx, rdx) and x86
(arguments on the stack). The symbolic execution must give an expression of the inputs with both ^ and &.

Usage: python tests/test_miasm.py
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fathom_miasm  # noqa: E402

CODE = {
    # mov eax, ecx; xor eax, edx; and ecx, edx; lea eax, [rax + rcx*2]; ret
    "x64": (0x140001000, bytes.fromhex("89c831d021d18d0448c3")),
    # mov eax, [esp+4]; mov ecx, [esp+8]; mov edx, eax; xor eax, ecx; and edx, ecx; lea eax, [eax + edx*2]; ret
    "x86": (0x401000, bytes.fromhex("8b4424048b4c240889c231c821ca8d0450c3")),
}


class Reader:
    def __init__(self, base: int, code: bytes):
        self.base, self.code = base, code

    def read(self, address: int, size: int) -> bytes:
        if not (self.base <= address and address + size <= self.base + 0x1000):
            raise OSError(f"unreadable {address:#x}")
        page = self.code + bytes(0x1000 - len(self.code))
        return page[address - self.base:address - self.base + size]


def fake_minidump(base: int, code: bytes):
    class Dump:
        @staticmethod
        def parse(_path):
            return Dump()

        def get_reader(self):
            return Reader(base, code)

    return Dump


def run(action, **kw) -> tuple[dict, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = action(argparse.Namespace(dump="fake.dmp", **kw))
    return result, out.getvalue()


failures = 0
for arch, (base, code) in CODE.items():
    fathom_miasm.MinidumpFile = fake_minidump(base, code)
    result, text = run(fathom_miasm.action_ir, address=base, arch=arch)
    ok = result["ok"] and result["blocks"] >= 1 and "^" in text and "&" in text
    print(("ok    " if ok else "FAIL  ") + f"{arch}: ir, {result['blocks']} blocks, {result['lines']} lines")
    failures += not ok
    result, text = run(fathom_miasm.action_symb, address=base, arch=arch, end=None, max_blocks=100)
    eax = next((line for line in text.splitlines() if line.strip().startswith(("RAX =", "EAX ="))), "")
    ok = result["ok"] and "^" in eax and "&" in eax and "return" in result["stopped"]
    print(("ok    " if ok else "FAIL  ") + f"{arch}: symb, stopped at {result['stopped']}: {eax.strip()}")
    failures += not ok

print("all passed" if not failures else f"{failures} failed")
sys.exit(1 if failures else 0)
