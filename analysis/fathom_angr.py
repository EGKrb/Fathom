"""angr on the debuggee: path exploration from the current state of a thread, loaded from a minidump of the
process (its memory and registers as they are now). Memory ranges are made symbolic, angr explores the
paths from the thread's instruction pointer, and the input that reaches FIND (without going through
AVOID) is solved.

    python fathom_angr.py explore DUMP --thread TID --find ADDR [--find ...] [--avoid ADDR ...]
                          --symbolic ADDR:SIZE [--symbolic ...] [--timeout S] [--result FILE]

Used by the angr.* commands of Fathom's ExtraCmds plugin. Needs angr (pip install angr).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time


def number(text: str) -> int:
    return int(text, 0)


def region(text: str) -> tuple[int, int]:
    address, size = text.split(":")
    return int(address, 0), int(size, 0)


def explore(args) -> dict:
    # before importing angr: its optional native engine (unicornlib) is reported at import
    logging.getLogger("angr.state_plugins.unicorn_engine").setLevel(logging.CRITICAL)
    import angr
    try:
        import claripy  # angr 9
    except ImportError:
        from angr import claripy  # angr 10: part of angr

    logging.getLogger("angr").setLevel(logging.ERROR)
    logging.getLogger("cle").setLevel(logging.ERROR)
    began = time.time()
    project = angr.Project(args.dump, main_opts={"backend": "minidump"}, auto_load_libs=False)
    dump = project.loader.main_object
    registers = dump.thread_registers(args.thread) if args.thread else dump.thread_registers()
    arch = project.arch
    pc = registers.get(arch.register_names[arch.ip_offset])
    print(f"loaded {args.dump} ({arch.name}) in {time.time() - began:.1f} s, thread {args.thread or 'first'} at {pc:#x}")

    state = project.factory.blank_state(addr=pc, add_options={
        angr.options.ZERO_FILL_UNCONSTRAINED_MEMORY, angr.options.ZERO_FILL_UNCONSTRAINED_REGISTERS})
    for name, value in registers.items():
        try:
            setattr(state.regs, name, value)
        except Exception:
            pass  # registers angr names differently (segments, debug registers)
    symbols = []
    for index, (address, size) in enumerate(args.symbolic):
        value = claripy.BVS(f"input{index}", size * 8)
        state.memory.store(address, value)
        symbols.append((address, size, value))
        print(f"symbolic: {size} bytes at {address:#x}")

    manager = project.factory.simulation_manager(state)
    deadline = began + args.timeout
    found = None
    steps = 0
    while manager.active and found is None and time.time() < deadline and steps < args.max_steps:
        manager.explore(find=args.find, avoid=args.avoid, n=1)
        steps += 1
        if manager.found:
            found = manager.found[0]
    print(f"{steps} steps, {len(manager.active)} active, {len(manager.deadended)} ended, "
          f"{len(manager.stashes.get('avoid', []))} avoided paths, {time.time() - began:.1f} s")
    if found is None:
        reason = "timeout" if time.time() >= deadline else "no path reaches the target"
        print(f"not found: {reason}")
        return dict(ok=True, found=False, reason=reason)
    solution = []
    for address, size, value in symbols:
        data = found.solver.eval(value, cast_to=bytes)
        text = "".join(chr(b) if 0x20 <= b < 0x7F else f"\\x{b:02X}" for b in data)
        print(f"solution: {size} bytes at {address:#x} = {data.hex()} \"{text}\"")
        solution.append(dict(address=f"{address:#x}", bytes=data.hex()))
    print(f"found a path to {found.addr:#x}")
    return dict(ok=True, found=True, address=f"{found.addr:#x}", solution=solution)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--result", help="write the result as JSON to this file")
    actions = parser.add_subparsers(dest="action", required=True)
    sub = actions.add_parser("explore")
    sub.add_argument("dump")
    sub.add_argument("--thread", type=number, default=0)
    sub.add_argument("--find", type=number, action="append", required=True)
    sub.add_argument("--avoid", type=number, action="append", default=[])
    sub.add_argument("--symbolic", type=region, action="append", default=[])
    sub.add_argument("--timeout", type=float, default=300)
    sub.add_argument("--max-steps", type=int, default=100000)
    sub.add_argument("--result", default=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        result = explore(args)
    except Exception as error:
        import traceback
        traceback.print_exc()
        result = dict(ok=False, error=f"{type(error).__name__}: {error}")
    if args.result:
        with open(args.result, "w", encoding="utf-8") as f:
            json.dump(result, f)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
