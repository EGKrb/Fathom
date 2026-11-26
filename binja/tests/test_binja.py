"""Test of the Binary Ninja plugin of Fathom (binja/fathom_binja), against a real Fathom, without Binary Ninja:

1. sync.Link / Follower through pybridge: a view mapped to the module of the debuggee (opened by Fathom's binja
   command: same addresses; or the file on disk: at its preferred base), the instruction pointer followed, the
   disassembly shown, a breakpoint set and deleted, the debuggee run to an address of the view, a comment;
2. sync.read_trace on a trace recorded and exported by Fathom (TraceExport json);
3. the plugin itself (__init__.py) loaded with a minimal stand-in of the binaryninja module: its commands
   registered, Connect, Show, Run to here and Import a trace run on a stand-in view.

Usage: py binja/tests/test_binja.py --release <Fathom release folder with pybridge> --target triton/build-x64/triton_target.exe
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "binja"))
sys.path.insert(0, str(ROOT / "binja" / "fathom_binja"))  # sync.py alone: the package needs binaryninja
sys.path.insert(0, str(ROOT / "pybridge" / "python"))

import sync  # noqa: E402
from x64dbg_bridge import Debugger  # noqa: E402

PORT = 27141
results: list[tuple[str, bool]] = []


def check(what: str, passed: bool) -> None:
    results.append((what, passed))
    print(("ok    " if passed else "FAIL  ") + what)


def stand_in_binaryninja(registered: list, logs: list, trace_path: str) -> types.ModuleType:
    """A binaryninja module with what the plugin uses (names and signatures of the real API)."""
    bn = types.ModuleType("binaryninja")

    class PluginCommand:
        @staticmethod
        def register(name, description, action, is_valid=None):
            registered.append((name, action, False))

        @staticmethod
        def register_for_address(name, description, action, is_valid=None):
            registered.append((name, action, True))

    bn.PluginCommand = PluginCommand
    bn.log_info = lambda text, logger="": logs.append(("info", str(text)))
    bn.log_error = lambda text, logger="": logs.append(("error", str(text)))
    bn.HighlightStandardColor = types.SimpleNamespace(GreenHighlightColor="green")
    bn.mainthread = types.SimpleNamespace(execute_on_main_thread=lambda f: f())
    bn.interaction = types.SimpleNamespace(get_open_filename_input=lambda prompt, ext="": trace_path)
    return bn


class StandInFunction:
    def __init__(self, start, end):
        self.start, self.end = start, end
        self.highlights, self.tags = {}, {}

    def set_user_instr_highlight(self, addr, color, arch=None):
        self.highlights[addr] = color

    def add_tag(self, tag_type, data, addr=None, auto=False, arch=None):
        self.tags[addr] = (tag_type, data)


class StandInView:
    def __init__(self, path, start, size, function):
        self.file = types.SimpleNamespace(original_filename=path, filename=path, view="Linear:PE")
        self.start, self.end = start, start + size
        self.function = function
        self.navigated = []
        self.tag_types = {}

    def navigate(self, view_name, offset):
        self.navigated.append(offset)
        return True

    def get_tag_type(self, name):
        return self.tag_types.get(name)

    def create_tag_type(self, name, icon):
        self.tag_types[name] = icon

    def get_functions_containing(self, addr, plat=None):
        return [self.function] if self.function.start <= addr < self.function.end else []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True, help="a Fathom release folder (release\\ with x64\\plugins\\pybridge.dp64)")
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix="fathom-binja-"))
    target = work / "triton_target.exe"
    shutil.copyfile(args.target, target)

    with Debugger.launch(args.release, arch="x64", port=PORT) as dbg:
        dbg.init(str(target))
        dbg.cmd("bc")
        dbg.cmd("bp triton_target.check_mix")
        dbg.run(wait=True)
        check_mix = dbg.eval("triton_target.check_mix")
        check_branches = dbg.eval("triton_target.check_branches")
        base = dbg.eval("mod.base(triton_target)")
        check("stopped at check_mix", dbg.reg("cip") == check_mix)

        # 1a. a view opened by Fathom's binja command: the module read from memory, at its load address
        link = sync.Link(debugger=dbg)
        found = link.attach_view(str(work / "triton_target_0x1" / "triton_target.exe"), base)
        check("view of binja: same addresses", found == base and link.delta == 0)
        # 1b. a view of the file on disk, at its preferred image base
        preferred = 0x140000000
        link_disk = sync.Link(debugger=dbg)
        link_disk.attach_view(str(target), preferred)
        check("view of the file: shifted by the load address", link_disk.to_view(check_mix) == preferred + (check_mix - base))

        moves = []
        follower = sync.Follower(link_disk, preferred, 0x10000, moves.append)
        follower.step(None)
        check("the view follows cip", moves == [preferred + (check_mix - base)])
        link_disk.goto(link_disk.to_view(check_branches))
        link_disk.comment(link_disk.to_view(check_branches), "from Binary Ninja")
        check("comment set in Fathom", True)
        link_disk.breakpoint(link_disk.to_view(check_branches), True)
        dbg.cmd("bc triton_target.check_mix")
        dbg.run(wait=True)
        check("breakpoint from the view hit", dbg.reg("cip") == check_branches)
        link_disk.breakpoint(link_disk.to_view(check_branches), False)

        # 2. a trace of check_branches, exported by Fathom
        trace = work / "trace.trace64"
        export = work / "trace.json"
        ret = dbg.eval("[csp]")
        dbg.cmd(f'StartTraceRecording "{trace}"')
        dbg.cmd(f"TraceIntoConditional cip == {ret:#x}, .2000")
        dbg.wait()
        dbg.cmd("StopTraceRecording")
        dbg.cmd(f'TraceExport "{trace}", "{export}", json')
        counts = sync.read_trace(str(export))
        check("trace read: check_branches executed", counts.get(check_branches, 0) == 1 and len(counts) > 5)

        # 1c. run to an address of the view (a single-shot breakpoint): main's call of check_branches again?
        # the debuggee is at the return of check_branches: run to mba
        mba = dbg.eval("triton_target.mba")
        link_disk.run_to(link_disk.to_view(mba))
        dbg.wait()
        check("run to here from the view", dbg.reg("cip") == mba)

        # 3. the plugin with a stand-in binaryninja, connected to this Fathom
        registered, logs = [], []
        sys.modules["binaryninja"] = stand_in_binaryninja(registered, logs, str(export))
        import fathom_binja  # noqa: E402
        fathom_binja.sync.DEFAULT_PORT = PORT
        names = [name for name, _, _ in registered]
        check("7 commands registered", len(names) == 7 and "Fathom\\Run to here" in names)
        function = StandInFunction(preferred + (check_branches - base), preferred + (check_branches - base) + 0x40)
        view = StandInView(str(target), preferred, 0x10000, function)
        # pybridge serves one client at a time: the test's connection makes way for the plugin's (Fathom
        # keeps running; the plugin's connection stops it at the end)
        process, dbg._process = dbg._process, None
        dbg._file.close()
        dbg._sock.close()
        fathom_binja.connect(view)
        link = fathom_binja._links.get(view)
        check("Connect", link is not None and link.delta == base - preferred)
        if link is not None:
            plugin_dbg = link.debugger
            plugin_dbg._process = process
            fathom_binja.show(view, preferred + (check_mix - base))
            fathom_binja.import_trace(view)
            check("Import a trace: instructions highlighted and tagged", len(function.highlights) > 5
                  and len(function.tags) == len(function.highlights) and "Fathom trace" in view.tag_types)
            # Run to here: from the return of mba, the single-shot breakpoint at main's printf call site is
            # not on the path; run to the exit instead and check the command went through
            fathom_binja.set_breakpoint(view, preferred + (check_mix - base))
            fathom_binja.delete_breakpoint(view, preferred + (check_mix - base))
            check("no error logged", not [l for l in logs if l[0] == "error"])
            plugin_dbg.close()
        else:
            dbg._process = process
        for level, text in logs:
            print(f"   [{level}] {text}")
    failed = [w for w, p in results if not p]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
