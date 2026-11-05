from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the order of a trace of every thread.")
    parser.add_argument("--log", required=True)
    parser.add_argument("--userdir", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--artifacts-dir", required=True)
    parser.add_argument("--current", action="store_true", help="TraceSetThreads current: only the main thread")
    return parser.parse_args()


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main() -> int:
    args = parse_args()
    runtime_dir = Path(args.runtime_dir)
    if not args.current and not (runtime_dir / "trace.trace64").exists():
        # TraceSetThreads all is not supported by the TitanEngine emulation of GleeBug: nothing recorded
        ini = Path(args.userdir) / "headless.ini"
        engine = ini.read_text(encoding="utf-8") if ini.is_file() else ""
        if "DebugEngine=0" in engine or "DebugEngine" not in engine:
            return fail("nothing was recorded with TitanEngine")
        print("TraceSetThreads all not supported by this debug engine, skipped")
        return 0
    try:
        lines = (runtime_dir / "workers.txt").read_text().split()
        counter, expected = int(lines[0], 16), int(lines[1])
        workers = {int(worker) for worker in lines[2:]}
        instructions = json.loads((runtime_dir / "trace.json").read_text(encoding="utf-8"))["instructions"]
    except (OSError, ValueError, KeyError, IndexError) as exc:
        return fail(f"cannot read the outputs: {exc}")

    recorded = {instruction["thread"] for instruction in instructions}
    if args.current:
        if len(recorded) != 1 or recorded & workers:
            return fail(f"threads {sorted(recorded)} recorded, expected only the main thread")
        if not any("skip" in instruction for instruction in instructions):
            return fail("no call ran untraced")
        print(f"{len(instructions)} instructions of the main thread")
        return 0
    if not workers <= recorded:
        return fail(f"workers {sorted(workers - recorded)} were not recorded")
    if not any("skip" in instruction for instruction in instructions):
        return fail("no call ran untraced")

    # The interlocked increments of the counter, in the global order of the trace. None may be
    # missing. When the records are in execution order, an increment sees the value written by the
    # previous one; two threads that execute an instruction at the same time on two processors are
    # only ordered by their debug events, and the value read after the first one can already include
    # the write of the second one: most increments must still follow each other exactly.
    increments = []
    for instruction in instructions:
        if not instruction["disasm"].startswith("lock "):
            continue
        for access in instruction.get("mem", []):
            if int(access["address"], 16) == counter:
                old = int(access["old"], 16) & 0xFFFFFFFF
                new = int(access["new"], 16) & 0xFFFFFFFF
                increments.append((old, new, instruction["thread"], instruction["seq"]))
    if len(increments) != expected:
        return fail(f"{len(increments)} increments recorded, expected {expected}")
    if max(new for _, new, _, _ in increments) != expected:
        return fail(f"the last value of the counter is not {expected}")
    exact = sum(1 for old, new, _, _ in increments if new == old + 1)
    chained = sum(1 for a, b in zip(increments, increments[1:]) if b[0] == a[1])
    if exact * 2 < expected or chained * 2 < expected - 1:
        return fail(f"{exact} exact increments and {chained} chained of {expected}: the records are not in execution order")
    print(f"{len(instructions)} instructions, {len(recorded)} threads, {len(increments)} increments: {exact} exact, {chained} chained")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
