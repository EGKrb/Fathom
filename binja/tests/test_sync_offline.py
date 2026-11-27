"""Test of fathom_binja/sync.py without Fathom and without Binary Ninja: a stand-in debugger records the commands
the plugin would send through pybridge (the mapping of a view to a module loaded elsewhere, breakpoints, run to
here, comments, following the instruction pointer) and a TraceExport json is counted. The test against a real
Fathom is tests/test_binja.py.

Usage: python tests/test_sync_offline.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fathom_binja"))
import sync  # noqa: E402

results: list[tuple[str, bool]] = []


def check(what: str, passed: bool) -> None:
    results.append((what, passed))
    print(("ok    " if passed else "FAIL  ") + what)


class StandInDebugger:
    """What sync.Link uses of pybridge's Debugger: eval, reg, cmd."""

    def __init__(self, modules: dict[str, int], cips: list[int]):
        self.modules, self.cips, self.commands = modules, list(cips), []

    def eval(self, expression: str) -> int:
        name = expression[len("mod.base("):-1]
        return self.modules.get(name, 0)

    def reg(self, name: str) -> int:
        assert name == "cip"
        return self.cips.pop(0) if len(self.cips) > 1 else self.cips[0]

    def cmd(self, command: str) -> bool:
        self.commands.append(command)
        return True


# the view: the file at its preferred base 0x140000000; the debuggee loaded it at 0x7ff6a0000000
debugger = StandInDebugger({"target": 0x7FF6A0000000}, [0x7FF6A0001010, 0x7FF6A0001010, 0x7FF6A0001020, 0x401000])
link = sync.Link(debugger=debugger)
base = link.attach_view(r"C:\Users\me\AppData\Local\Temp\Fathom\binja\target_7ff6a0000000\target.exe", 0x140000000)
check("view mapped to the module by its name without extension", base == 0x7FF6A0000000)
check("addresses mapped both ways", link.to_debuggee(0x140001000) == 0x7FF6A0001000 and link.to_view(0x7FF6A0001000) == 0x140001000)

link.goto(0x140001000)
link.breakpoint(0x140001010)
link.breakpoint(0x140001010, enable=False)
link.run_to(0x140001020)
link.comment(0x140001000, 'say "hi" \\o/')
check("commands sent at the debuggee's addresses", debugger.commands == [
    "disasm 0x7ff6a0001000", "bp 0x7ff6a0001010", "bc 0x7ff6a0001010", "run 0x7ff6a0001020",
    'cmt 0x7ff6a0001000, "say \\"hi\\" \\\\o/"'])

try:
    sync.Link(debugger=StandInDebugger({}, [0])).attach_view("missing.dll", 0x180000000)
    check("a module the debuggee lacks is reported", False)
except LookupError:
    check("a module the debuggee lacks is reported", True)

moves: list[int] = []
follower = sync.Follower(link, 0x140000000, 0x10000, moves.append)
last = None
for _ in range(4):
    last = follower.step(last)
check("following: navigates when cip moves inside the module, not outside it", moves == [0x140001010, 0x140001020])

with tempfile.TemporaryDirectory() as folder:
    path = os.path.join(folder, "trace.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"instructions": [{"address": "7ff6a0001000"}, {"address": 0x7FF6A0001004},
                                    {"address": "7ff6a0001000"}, {"bytes": "90"}]}, f)
    counts = sync.read_trace(path)
check("trace: executions counted per address", counts == {0x7FF6A0001000: 2, 0x7FF6A0001004: 1})

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fathom_binja", "plugin.json"), encoding="utf-8") as f:
    meta = json.load(f)
check("plugin.json is valid", meta["pluginmetadataversion"] == 2 and meta["api"] == ["python3"])

failed = [what for what, passed in results if not passed]
print("all passed" if not failed else f"{len(failed)} failed")
sys.exit(1 if failed else 0)
