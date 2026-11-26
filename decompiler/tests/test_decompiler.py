"""Test of the Decompiler plugin (Ghidra's decompiler on the debuggee) in headless.exe, on tests/target.c.

The three functions of the target are decompiled from the memory of the running process, and their C is
checked: the loop and the constants of checksum, the cases of describe's switch (its string literals,
read from read-only memory), and in greet the Windows functions called through the import table, named
after x64dbg's symbols. The C of greet is also written to a file.

Usage: py decompiler/tests/test_decompiler.py --bin bin/x64 --build decompiler/build-x64
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin", required=True, help="bin/x64 or bin/x32 of the Fathom build (headless.exe)")
    parser.add_argument("--build", required=True, help="decompiler/build-x64 or build-x32")
    args = parser.parse_args()
    bindir = Path(args.bin).resolve()
    build = Path(args.build).resolve()
    x64 = (build / "Decompiler.dp64").exists()
    plugin = build / ("Decompiler.dp64" if x64 else "Decompiler.dp32")

    folder = bindir / "tests" / "decompiler"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    shutil.copyfile(plugin, folder / plugin.name)
    shutil.copytree(build / "decompiler", folder / "decompiler")
    shutil.copyfile(build / "decompiler_target.exe", folder / "decompiler_target.exe")

    script = '''settingset Events, TlsCallbacks, 0
settingset Events, TlsCallbacksSystem, 0
settingset Events, EntryBreakpoint, 1
init "tests/decompiler/decompiler_target.exe"
run
testassert $pid != 0, "stopped at entry"
decompile decompiler_target.checksum, "tests/decompiler/checksum.c"
testassert $result > 3, "checksum decompiled"
decompile decompiler_target.describe, "tests/decompiler/describe.c"
testassert $result > 3, "describe decompiled"
decompile decompiler_target.greet, "tests/decompiler/greet.c"
testassert $result > 3, "greet decompiled"
decompile.raw decompiler_target.checksum
testassert $result > 5, "raw listing"
run
'''
    (folder / "decompiler.txt").write_text(script, encoding="utf-8")
    log_path = folder / "decompiler.log"
    command = [str(bindir / "headless.exe"), "-testing", "-plugin", f"tests/decompiler/{plugin.name}",
               "-c", 'RedirectLog "tests/decompiler/decompiler.log"', "-cf", "tests/decompiler/decompiler.txt"]
    subprocess.run(command, cwd=bindir, timeout=600, capture_output=True)
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    final = ([line for line in log.splitlines() if "[x64dbg-test] FINAL" in line] or ["no FINAL line"])[-1]
    asserts = script.count("testassert")
    print(final, f"({asserts} assertions in the script)")
    ok = "status=pass" in final and f"asserts={asserts}" in final

    def function(name: str) -> str:
        # the C of each function, written to a file by the command (the log interleaves other messages)
        path = folder / f"{name}.c"
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    checks = {
        "checksum: the initial value 0x1505": "0x1505" in function("checksum"),
        "checksum: the multiplication by 33": bool(re.search(r"\* 0x21|\* 33|<< 5|0x21 \*", function("checksum"))),
        "describe: the string literals of the switch": all(word in function("describe") for word in ("zero", "three", "many")),
        "greet: wsprintfA through the import table": "wsprintfA" in function("greet"),
        "greet: OutputDebugStringA": "OutputDebugStringA" in function("greet"),
        "greet: GetCurrentProcessId": "GetCurrentProcessId" in function("greet"),
    }
    for what, passed in checks.items():
        print(("ok    " if passed else "FAIL  ") + what)
        ok = ok and passed
    for name in ("checksum", "describe", "greet"):
        print(f"--- {name}\n{function(name).strip()}")
    if not ok:
        print("log:", log_path)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
