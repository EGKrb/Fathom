"""Test of the emulation commands of ExtraCmds (emu.*), in headless.exe with the real emulator.

The debuggee (tests/trace_export.exe, stopped at its entry point) is saved to a minidump and its code
runs in dumpulator: Windows functions called with integer, string and buffer arguments, a system
call (VirtualAlloc), the thread run on until the process exits, a Python script, a recorded trace
opened in Time Travel, and a minidump emulated after the debugging session has ended. The debuggee
itself must not move.

Needs Python with the emulator's packages (emulator/requirements.txt) on the PATH.

Usage: py extracmds/tests/test_emulator.py --bin bin/x64 --plugin extracmds/build-x64-noqt/ExtraCmds.dp64
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

SCRIPT = '''# emu.script test: dumpulator's API from a script, with the argument of the command
text = dp.allocate(32)
dp.write(text, b"dumpulator\\0")
lstrlen = dp.modules.resolve_export("kernel32.dll", "lstrlenA").address
result["return"] = dp.call(lstrlen, [text]) + int(args[0], 0)
print(f"lstrlenA in the script: {result['return'] - int(args[0], 0)}")
'''


def run_headless(bindir: Path, script: str, plugin: Path) -> tuple[str, int, str]:
    folder = bindir / "tests" / "emulator"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.iterdir():
        if old.is_file():
            old.unlink()
    (folder / "script.py").write_text(SCRIPT, encoding="utf-8")
    (folder / "test.txt").write_text(script, encoding="utf-8")
    shutil.copyfile(plugin, folder / plugin.name)
    command = [str(bindir / "headless.exe"), "-testing", "-plugin", f"tests/emulator/{plugin.name}",
               "-c", 'RedirectLog "tests/emulator/test.log"', "-cf", "tests/emulator/test.txt"]
    subprocess.run(command, cwd=bindir, timeout=900, capture_output=True)
    log = folder / "test.log"
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    final = [line for line in text.splitlines() if "[x64dbg-test] FINAL" in line]
    return (final[-1] if final else "no FINAL line"), script.count("testassert"), text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin", required=True, help="bin/x64 or bin/x32 of the Fathom build")
    parser.add_argument("--plugin", required=True, help="ExtraCmds.dp64/.dp32 built without Qt")
    args = parser.parse_args()
    bindir = Path(args.bin).resolve()
    plugin = Path(args.plugin).resolve()

    script = r'''settingset Events, TlsCallbacks, 0
settingset Events, TlsCallbacksSystem, 0
settingset Events, EntryBreakpoint, 1
init tests/trace_export.exe
run
testassert $pid != 0, "stopped at entry"
var entry
mov entry, cip
var stacktop
mov stacktop, [csp]

emu.call ntdll.RtlComputeCrc32, 0, str:hello, 5
testassert $result == 0x3610A686, "RtlComputeCrc32 of a string argument"
emu.call kernel32.lstrlenW, wstr:Fathom
testassert $result == 6, "lstrlenW of a UTF-16 argument"
emu.call kernel32.GetCurrentProcessId
testassert $result == $pid, "the process id in the emulator"
emu.call kernel32.VirtualAlloc, 0, 0x10000, 0x3000, 4
testassert $result != 0, "VirtualAlloc (NtAllocateVirtualMemory emulated)"
emu.call kernel32.lstrcpyA, buf:32, str:copied
testassert $result != 0, "lstrcpyA into a buffer argument"

emu.trace on
emu.call ntdll.RtlComputeCrc32, 0, str:hello, 5
testassert $result == 0x3610A686, "the same result with the trace"
var traced
mov traced, $result1
testassert traced > 0x10, "instructions recorded"
tt.seek 0
tt.regs
testassert $result == ntdll.RtlComputeCrc32, "the trace starts at the called function"
emu.trace off

emu.syscalls on
emu.run 0, .500
testassert $result != entry, "the thread ran on from the entry point"
testassert $result2 == -1, "500 instructions: the process has not exited"
emu.syscalls off

emu.script "tests/emulator/script.py", 7
testassert $result == 0x11, "a script: lstrlenA(dumpulator) + its argument"

testassert cip == entry, "the debuggee did not move"
testassert [csp] == stacktop, "the memory of the debuggee did not change"

emu.dump "tests/emulator/saved.dmp"
stop
emu.load "tests/emulator/saved.dmp"
emu.call ntdll!RtlComputeCrc32, 0, str:hello, 5
testassert $result == 0x3610A686, "a minidump emulated without debugging"
emu.unload
'''
    final, asserts, log = run_headless(bindir, script, plugin)
    print(final, f"({asserts} assertions in the script)")
    if "status=pass" not in final or f"asserts={asserts}" not in final:
        for line in log.splitlines():
            if "testassert" in line.lower() or "ExtraCmds" in line or "Error" in line or "error" in line:
                print("  ", line)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
