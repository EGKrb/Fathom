"""Test of the Time Travel commands of ExtraCmds (tt.*) on a real trace, in headless.exe.

1. records a trace of tests/trace_export.exe and exports it as JSON (the reference) and as .ftt;
2. generates a script of tt.* commands whose expected results come from the JSON: positions,
   instruction pointer, steps forward / backward in the timeline and in a thread, another thread at
   the same instant, memory before / after a write (rebuilt from snapshots and deltas), backward
   searches of an execution and of a write;
3. runs it with the commands-only ExtraCmds plugin (headless.exe skips Qt plugins).

Usage: py extracmds/tests/test_time_travel.py --bin bin/x64 --plugin extracmds/build-x64-noqt/ExtraCmds.dp64
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def run_headless(bindir: Path, script: str, plugin: Path | None, name: str) -> tuple[str, int]:
    folder = bindir / "tests" / "time_travel"
    folder.mkdir(parents=True, exist_ok=True)
    script_path = folder / f"{name}.txt"
    script_path.write_text(script, encoding="utf-8")
    log = folder / f"{name}.log"
    log.unlink(missing_ok=True)
    command = [str(bindir / "headless.exe"), "-testing"]
    if plugin:
        local = folder / plugin.name
        shutil.copyfile(plugin, local)
        command += ["-plugin", f"tests/time_travel/{plugin.name}"]
    command += ["-c", f'RedirectLog "tests/time_travel/{name}.log"', "-cf", f"tests/time_travel/{name}.txt"]
    subprocess.run(command, cwd=bindir, timeout=600, capture_output=True)
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    final = [line for line in text.splitlines() if "[x64dbg-test] FINAL" in line]
    return (final[-1] if final else "no FINAL line"), script.count("testassert")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin", required=True, help="bin/x64 or bin/x32 of the Fathom build")
    parser.add_argument("--plugin", required=True, help="ExtraCmds.dp64/.dp32 built without Qt")
    args = parser.parse_args()
    bindir = Path(args.bin).resolve()
    plugin = Path(args.plugin).resolve()

    # StartTraceRecording appends to an existing trace: start from an empty folder. Two sessions are
    # recorded into the same trace on purpose, so that it holds two threads (x64dbg's single-step
    # tracer follows one thread, and the target has only one): the second one starts with a
    # context switch, which the per-thread timelines and "another thread at the same instant" use.
    folder = bindir / "tests" / "time_travel"
    if folder.exists():
        for old in folder.glob("tt.*"):
            old.unlink()
    record = r'''settingset Events, TlsCallbacks, 0
settingset Events, TlsCallbacksSystem, 0
settingset Events, EntryBreakpoint, 1
init tests/trace_export.exe
run
testassert $pid != 0, "stopped at entry"
StartTraceRecording "tests/time_travel/tt.trace64"
TraceIntoConditional 0, .300
StopTraceRecording
stop
init tests/trace_export.exe
run
testassert $pid != 0, "second session stopped at entry"
StartTraceRecording "tests/time_travel/tt.trace64"
TraceIntoConditional 0, .200
StopTraceRecording
TraceExport "tests/time_travel/tt.trace64", "tests/time_travel/tt.json", json, regs=all
testassert $result > .100, "reference json"
TraceExport "tests/time_travel/tt.trace64", "tests/time_travel/tt.ftt", ftt, regs=all, snapshot=.32
testassert $result > .100, "ftt with frequent snapshots"
run
'''
    final, asserts = run_headless(bindir, record, None, "record")
    print("record:", final)
    if "status=pass" not in final:
        return 1

    trace = json.loads((bindir / "tests" / "time_travel" / "tt.json").read_text(encoding="utf-8"))
    ins = trace["instructions"]
    n = len(ins)
    ip = "rip" if trace["arch"] == "x64" else "eip"
    ptr = 8 if trace["arch"] == "x64" else 4
    lines = ['tt.open "tests/time_travel/tt.ftt"', f'testassert $result == {n:#x}, "instruction count"']

    def at(k: int, what: str) -> None:
        lines.append(f"tt.seek {k:#x}")
        lines.append(f'testassert $result == {k:#x}, "seek {what}"')
        lines.append("tt.regs")
        lines.append(f'testassert $result == {ins[k]["address"]}, "{ip} at {k} ({what})"')

    for k in sorted({0, 1, n // 3, n // 2, n - 1}):
        at(k, "sample")
    lines += ["tt.seek 0x10", "tt.next 5", 'testassert $result == 0x15, "next 5"', "tt.prev 3", 'testassert $result == 0x12, "prev 3"']

    # steps inside a thread, and another thread at the same instant
    by_thread: dict[int, list[int]] = {}
    for i in ins:
        by_thread.setdefault(i["thread"], []).append(i["seq"])
    threads = sorted(by_thread, key=lambda t: -len(by_thread[t]))
    main_thread = threads[0]
    seqs = by_thread[main_thread]
    if len(seqs) >= 3:
        lines += [f"tt.seek {seqs[0]:#x}", "tt.tnext 2", f'testassert $result == {seqs[2]:#x}, "two steps in thread {main_thread}"',
                  "tt.tprev", f'testassert $result == {seqs[1]:#x}, "one step back in thread {main_thread}"']
    if len(threads) > 1:
        other = threads[1]
        when = by_thread[other][0]
        before = max([s for s in seqs if s < when], default=None)
        if before is not None:
            # at the instant `before` (main thread), the other thread is about to run its first instruction
            lines += [f"tt.seek {before:#x}", f"tt.thread {other:#x}", "tt.regs",
                      f'testassert $result == {ins[when]["address"]}, "thread {other} seen at the instant of thread {main_thread}"']

    # memory: before / after a write, rebuilt from the snapshots and the deltas
    writes = [(i["seq"], m) for i in ins for m in i["mem"] if int(m["old"], 16) != int(m["new"], 16)]
    checked = 0
    for seq, m in writes:
        if seq + 1 >= n or checked >= 3:
            continue
        address = int(m["address"], 16)
        lines += [f"tt.seek {seq + 1:#x}", f"tt.mem {address:#x}, {ptr:#x}", f'testassert $result == {m["new"]}, "memory after the write of {seq}"',
                  f"tt.seek {seq:#x}", f"tt.mem {address:#x}, {ptr:#x}", f'testassert $result == {m["old"]}, "memory before the write of {seq}"']
        last = max(s for s, w in writes if int(w["address"], 16) == address)
        if last < n - 1:
            lines += [f"tt.seek {n - 1:#x}", f"tt.write {address:#x}, {ptr:#x}", f'testassert $result == {last:#x}, "last write to the address"']
        checked += 1

    # backward search of an execution
    target = ins[n // 2]["address"]
    previous = max(i["seq"] for i in ins[:n - 1] if i["address"] == target)
    lines += [f"tt.seek {n - 1:#x}", f"tt.findback {target}", f'testassert $result == {previous:#x}, "previous execution of {target}"']
    lines.append("tt.close")

    final, asserts = run_headless(bindir, "\n".join(lines) + "\n", plugin, "navigate")
    print("navigate:", final, f"({asserts} assertions in the script)")
    return 0 if "status=pass" in final and f"asserts={asserts}" in final else 1


if __name__ == "__main__":
    raise SystemExit(main())
