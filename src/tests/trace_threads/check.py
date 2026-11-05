from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the CPU time samples of a multithreaded trace.")
    parser.add_argument("--log", required=True)
    parser.add_argument("--userdir", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--artifacts-dir", required=True)
    parser.add_argument("--mode", choices=("busy", "wait"), default="busy")
    return parser.parse_args()


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def samples(path: Path) -> list[tuple[int, dict]]:
    """The (seq, cpu) of the instructions that follow a CPU time sample."""
    document = json.loads(path.read_text(encoding="utf-8"))
    return [(instruction["seq"], instruction["cpu"]) for instruction in document["instructions"] if "cpu" in instruction]


def main() -> int:
    args = parse_args()
    runtime_dir = Path(args.runtime_dir)
    mode = args.mode
    try:
        worker = int((runtime_dir / f"worker.{mode}.txt").read_text().split()[0])
        document = json.loads((runtime_dir / f"trace.{mode}.json").read_text(encoding="utf-8"))
        from_ftt = samples(runtime_dir / f"trace.{mode}.ftt.json")
    except (OSError, ValueError, KeyError, IndexError) as exc:
        return fail(f"cannot read the outputs: {exc}")

    instructions = document["instructions"]
    recorded = {instruction["thread"] for instruction in instructions}
    if worker in recorded:
        return fail(f"the worker {worker} was recorded, only the main thread is traced")
    sampled = [(instruction["seq"], instruction["cpu"]) for instruction in instructions if "cpu" in instruction]
    if len(sampled) < 2:
        return fail(f"{len(sampled)} CPU time samples, expected one every 4096 instructions")
    if sampled != from_ftt:
        return fail("the samples of the .ftt differ from those of the .trace64")

    previous = None
    for seq, cpu in sampled:
        if not 0 <= cpu["from"] < seq or cpu["interval"] <= 0:
            return fail(f"sample before #{seq}: bad interval {cpu}")
        # the recorder samples every 4096 instructions: the intervals follow each other
        if previous is not None and cpu["from"] != previous:
            return fail(f"sample before #{seq} starts at #{cpu['from']}, the previous one ended at #{previous}")
        previous = seq
        for thread in list(cpu["outside"]) + list(cpu["kernel"]):
            if int(thread) in recorded:
                return fail(f"sample before #{seq}: the recorded thread {thread} is listed as running outside the trace")

    running = [seq for seq, cpu in sampled if str(worker) in cpu["outside"]]
    if mode == "busy" and len(running) * 2 < len(sampled):
        return fail(f"the spinning worker ran outside the trace in only {len(running)} of {len(sampled)} intervals")
    if mode == "wait":
        if running:
            return fail(f"the blocked worker is listed as executing code outside the trace before #{running[0]}")
        # the other threads (loader thread pool) only wake in the kernel for the suspensions
        concurrent = [seq for seq, cpu in sampled if cpu["outside"]]
        if concurrent:
            return fail(f"threads executed code outside the trace before #{concurrent[0]}: the order should be exact")
    print(f"{mode}: {len(sampled)} samples, worker {worker} outside the trace in {len(running)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
