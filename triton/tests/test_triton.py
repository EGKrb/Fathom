"""Test of the Triton plugin (sym.* / taint.* commands) in headless.exe, on tests/target.c.

1. the debuggee: at the entry of check_mix, its serial is made symbolic (and tainted), Triton runs the
   function to its return, Z3 finds the serial for which it returns 1, sym.apply writes it into the
   debuggee, and the debuggee itself then runs check_mix natively: it returns 1;
2. check_branches: the first branch is flipped (sym.flip), the solution applied, and a second run goes
   one comparison further;
3. mba(x, y) = (x ^ y) + 2 * (x & y): sym.synth finds x + y;
4. a recorded trace of check_mix (.trace64, converted to .ftt by sym.trace) is replayed by Triton, and
   the serial solved from the trace.

Usage: py triton/tests/test_triton.py --bin bin/x64 --build triton/build-x64
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path


def run_headless(bindir: Path, script: str, plugin: Path, name: str) -> tuple[str, str]:
    folder = bindir / "tests" / "triton"
    script_path = folder / f"{name}.txt"
    script_path.write_text(script, encoding="utf-8")
    log = folder / f"{name}.log"
    log.unlink(missing_ok=True)
    command = [str(bindir / "headless.exe"), "-testing", "-plugin", f"tests/triton/{plugin.name}",
               "-c", f'RedirectLog "tests/triton/{name}.log"', "-cf", f"tests/triton/{name}.txt"]
    subprocess.run(command, cwd=bindir, timeout=900, capture_output=True)
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    final = [line for line in text.splitlines() if "[x64dbg-test] FINAL" in line]
    return (final[-1] if final else "no FINAL line"), text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin", required=True, help="bin/x64 or bin/x32 of the Fathom build (headless.exe)")
    parser.add_argument("--build", required=True, help="triton/build-x64 or build-x32 (Triton.dp64/.dp32, libz3.dll, triton_target.exe)")
    args = parser.parse_args()
    bindir = Path(args.bin).resolve()
    build = Path(args.build).resolve()
    x64 = (build / "Triton.dp64").exists()
    plugin = build / ("Triton.dp64" if x64 else "Triton.dp32")

    folder = bindir / "tests" / "triton"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    for name in (plugin.name, "libz3.dll", "triton_target.exe") + (() if x64 else ("vm_target.exe",)):
        shutil.copyfile(build / name, folder / name)

    # arguments: rcx / rdx on x64, the stack on x86
    serial = "rcx" if x64 else "[esp+4]"
    sp = "rsp" if x64 else "esp"
    mba_args = (["sym.reg ecx, x", "sym.reg edx, y"] if x64 else
                ["sym.memvar esp+4, 4, x", "sym.memvar esp+8, 4, y"])
    script = f'''settingset Events, TlsCallbacks, 0
settingset Events, TlsCallbacksSystem, 0
settingset Events, EntryBreakpoint, 1
init "tests/triton/triton_target.exe", "AAAAAAAA"
run
testassert $pid != 0, "stopped at entry"

// 1. check_mix: solve the serial, apply it, run the function natively
bp triton_target.check_mix
run
testassert cip == triton_target.check_mix, "at check_mix"
sym.start
sym.mem {serial}, 8, serial
taint.mem {serial}, 8
sym.run
testassert $result1 > 10, "check_mix emulated by Triton"
sym.expr eax
testassert $result1 == 1, "eax is symbolic"
sym.list
testassert $result > 0, "instructions on symbolic / tainted data"
sym.solve eax, 1
testassert $result == 1, "serial found"
sym.apply
testassert $result == 8, "serial written into the debuggee"
bc triton_target.check_mix
rtr
testassert eax == 1, "the debuggee accepts the serial natively"

// 2. check_branches: flip the first branch, apply, the next run goes one comparison further
bp triton_target.check_branches
run
testassert cip == triton_target.check_branches, "at check_branches"
sym.start
sym.mem {serial}, 8, s
sym.run
sym.branches
testassert $result >= 1, "symbolic branches"
sym.flip 0
testassert $result == 1, "first branch flipped"
sym.apply
sym.start
sym.mem {serial}, 8, s
sym.run
sym.branches
testassert $result >= 2, "one more comparison after the flip"
bc triton_target.check_branches

// 3. mba: synthesis of x + y
bp triton_target.mba
run
testassert cip == triton_target.mba, "at mba"
sym.start
{chr(10).join(mba_args)}
sym.run
sym.expr eax
testassert $result1 == 1, "mba result is symbolic"
sym.synth eax
testassert $result == 1, "mba synthesized"
sym.stop
bc triton_target.mba
stop

// 4. a recorded trace of check_mix, replayed by Triton
init "tests/triton/triton_target.exe", "AAAAAAAA"
run
bp triton_target.check_mix
run
testassert cip == triton_target.check_mix, "at check_mix (trace)"
var ret
mov ret, [{sp}]
StartTraceRecording "tests/triton/mix.trace64"
TraceIntoConditional cip == ret, .5000
StopTraceRecording
sym.trace "tests/triton/mix.trace64"
sym.mem {serial}, 8, serial
sym.run ret
testassert $result == ret, "trace replayed to the return of check_mix"
sym.expr eax
testassert $result1 == 1, "eax is symbolic in the trace"
sym.solve eax, 1
testassert $result == 1, "serial found from the trace"
sym.info
sym.stop

// 5. vm.detect: no virtual machine in check_mix (a context save is more than a prologue)
vm.detect "tests/triton/mix.trace64"
testassert $result == 0, "no virtual machine in check_mix"
run
'''
    final, log = run_headless(bindir, script, plugin, "triton")
    vm_ok = True
    if not x64:
        # a virtual machine (tests/vm_target.c): detected in its trace, its computation recovered
        vm_script = '''settingset Events, EntryBreakpoint, 1
init "tests/triton/vm_target.exe"
run
bp vm_target.run_vm
run
testassert cip == vm_target.run_vm, "at run_vm"
var ret
mov ret, [esp]
StartTraceRecording "tests/triton/vm.trace32"
TraceIntoConditional cip == ret, .5000
StopTraceRecording
vm.detect "tests/triton/vm.trace32"
testassert $result == 1, "one virtual machine detected"
vm.simplify 0
testassert $result >= 1, "registers computed by the virtual machine"
sym.stop
run
'''
        vm_final, vm_log = run_headless(bindir, vm_script, plugin, "vm")
        vm_asserts = vm_script.count("testassert")
        print("vm:", vm_final, f"({vm_asserts} assertions)")
        eax = re.search(r"\[Triton\]   eax = (.*)\n(?:.*equivalent to (.*))?", vm_log)
        shown = (eax.group(2) or eax.group(1)) if eax else ""
        print("virtualized eax:", shown or "(not found)")
        vm_ok = ("status=pass" in vm_final and f"asserts={vm_asserts}" in vm_final and "eax_in" in shown and "ebx_in" in shown
                 and "0x5a" in shown.lower())
    asserts = script.count("testassert")
    print(final, f"({asserts} assertions in the script)")
    ok = "status=pass" in final and f"asserts={asserts}" in final
    # the synthesized expression of mba: x + y
    synth = re.search(r"eax is equivalent to:\s*\n(.*)", log)
    shown = synth.group(1).strip() if synth else ""
    print("mba synthesized:", shown or "(not found)")
    if not (("x + y" in shown) or ("y + x" in shown)):
        ok = False
    solved = [line.strip() for line in log.splitlines() if "bytes at" in line and "[Triton]" in line]
    for line in solved:
        print(line)
    ok = ok and vm_ok
    if not ok:
        print("log:", folder / "triton.log")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
