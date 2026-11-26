"""Test of the analysis commands of ExtraCmds (novmp, miasm.*, angr.*) in headless.exe, on the target of the
Triton plugin (triton/tests/target.c):

1. miasm.ir on check_mix (its blocks lifted to miasm's IR) and miasm.symb on mba (what it computes);
2. angr: at the entry of check_branches, the serial is made symbolic, angr explores the paths to the
   "return 1" of the function (avoiding the "return 0"), the input it finds is written into the debuggee,
   and the debuggee runs check_branches natively: it returns 1;
3. novmp (x64): the module image read from memory and given to NoVmp, which finds no VMProtect virtual
   machine in it (the target is not protected); vtildump prints a routine lifted from VMProtect (VTIL's
   sample, tests/vmprotect.vtil of novmp).

Usage: py extracmds/tests/test_tools.py --bin bin/x64 --plugin extracmds/build-x64-noqt/ExtraCmds.dp64
       --target triton/build-x64/triton_target.exe [--python <python with angr>]
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin", required=True)
    parser.add_argument("--plugin", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--python", help="the Python the commands use (first on the PATH): with angr, future, pyparsing")
    args = parser.parse_args()
    bindir = Path(args.bin).resolve()
    x64 = args.plugin.endswith(".dp64")
    folder = bindir / "tests" / "tools"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    plugin = Path(args.plugin).resolve()
    shutil.copyfile(plugin, folder / plugin.name)
    shutil.copyfile(args.target, folder / "triton_target.exe")

    serial = "rcx" if x64 else "[esp+4]"
    found, avoid = ("0x2A", "0x30") if x64 else ("0x20", "0x25")
    script = f'''settingset Events, TlsCallbacks, 0
settingset Events, EntryBreakpoint, 1
init "tests/tools/triton_target.exe", "AAAAAAAA"
run
testassert $pid != 0, "stopped at entry"
bc

// 1. miasm
miasm.ir triton_target.check_mix
testassert $result > 0, "check_mix lifted to miasm IR"
miasm.symb triton_target.mba
testassert $result > 0, "mba executed symbolically by miasm"

// 2. angr
bp triton_target.check_branches
run
testassert cip == triton_target.check_branches, "at check_branches"
angr.symbolic {serial}, 8
angr.explore triton_target.check_branches + {found}, triton_target.check_branches + {avoid}
testassert $result == 1, "angr found the serial"
angr.apply
testassert $result == 8, "serial written"
bc triton_target.check_branches
rtr
testassert eax == 1, "the debuggee accepts angr's serial natively"
'''
    if x64:
        script += '''
// 3. novmp
novmp triton_target.check_mix
testassert $result == 0, "no VMProtect virtual machine in the target"
'''
    script += "run\n"
    (folder / "tools.txt").write_text(script, encoding="utf-8")
    env = dict(os.environ)
    if args.python:
        env["PATH"] = str(Path(args.python).resolve().parent) + os.pathsep + env["PATH"]
    log_path = folder / "tools.log"
    command = [str(bindir / "headless.exe"), "-testing", "-plugin", f"tests/tools/{plugin.name}",
               "-c", 'RedirectLog "tests/tools/tools.log"', "-cf", "tests/tools/tools.txt"]
    subprocess.run(command, cwd=bindir, timeout=1800, capture_output=True, env=env)
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    final = ([line for line in log.splitlines() if "[x64dbg-test] FINAL" in line] or ["no FINAL line"])[-1]
    asserts = script.count("testassert")
    print(final, f"({asserts} assertions in the script)")
    ok = "status=pass" in final and f"asserts={asserts}" in final
    for pattern in (r"solution: .*", r"^\s*(RAX|EAX) = .*", r"no VMProtect.*", r"NoVmp on .*"):
        for match in re.finditer(pattern, log, re.M):
            print("  " + match.group(0).strip())
            break

    if x64:
        # vtildump on a routine lifted from VMProtect (VTIL's sample)
        vtildump = ROOT / "novmp" / "build" / "vtildump.exe"
        sample = ROOT / "novmp" / "tests" / "vmprotect.vtil"
        run = subprocess.run([str(vtildump), str(sample)], capture_output=True, text=True, errors="replace", timeout=300)
        lines = run.stdout.count("\n")
        print(f"vtildump of the VMProtect sample: exit {run.returncode}, {lines} lines")
        ok = ok and run.returncode == 0 and lines > 100 and "blocks" in run.stdout
    if not ok:
        print("log:", log_path)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
