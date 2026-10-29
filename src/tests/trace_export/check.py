from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the files written by the TraceExport command.")
    parser.add_argument("--log", required=True)
    parser.add_argument("--userdir", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--artifacts-dir", required=True)
    return parser.parse_args()


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


TextLine = tuple[dict[str, int], list[tuple[str, int, bytes]]]


def parse_text_trace(path: Path) -> tuple[int | None, list[TextLine]]:
    """Read a Tenet/Frinet text trace: the ASLR slide and, per line, the registers and memory accesses."""
    text = path.read_bytes().decode("ascii")
    if "\r" in text or not text.endswith("\n"):
        raise ValueError("lines must end with LF, without CR")
    slide = None
    lines: list[TextLine] = []
    for number, raw in enumerate(text.split("\n")[:-1]):
        regs: dict[str, int] = {}
        mem: list[tuple[str, int, bytes]] = []
        for item in raw.split(","):
            name, value = item.split("=")
            if not value.startswith("0x"):
                raise ValueError(f"line {number}: {item!r} is not a 0x-prefixed value")
            if name in ("mr", "mw"):
                address, data = value.split(":")
                mem.append((name, int(address, 16), bytes.fromhex(data)))
            else:
                regs[name] = int(value, 16)
        if number == 0 and list(regs) == ["slide"] and not mem:
            slide = regs["slide"]
            continue
        lines.append((regs, mem))
    return slide, lines


def expected_memory(instruction: dict, ptr_size: int) -> list[tuple[str, int, bytes]]:
    """Memory accesses of an instruction as a text trace stores them: content after execution."""
    result = []
    for access in instruction["mem"]:
        old, new = int(access["old"], 16), int(access["new"], 16)
        kind = "mr" if old == new else "mw"
        result.append((kind, int(access["address"], 16), new.to_bytes(ptr_size, "little")))
    return result


def check_tenet(path: Path, instructions: list[dict], gprs: list[str], ip: str, ptr_size: int) -> str | None:
    """Each Tenet line holds the state before its instruction and the memory accessed by the previous one."""
    _, lines = parse_text_trace(path)
    if len(lines) != len(instructions):
        return f"{path.name} has {len(lines)} lines, json has {len(instructions)} instructions"
    state: dict[str, int] = {}
    for index, ((regs, mem), instruction) in enumerate(zip(lines, instructions)):
        if not regs or list(regs)[-1] != ip or set(regs) - set(gprs) - {ip}:
            return f"{path.name} line {index}: unexpected registers {list(regs)}"
        state.update(regs)
        expected = {name: int(instruction["regs"][name], 16) for name in gprs + [ip]}
        if state != expected:
            return f"{path.name} line {index}: registers do not match json"
        if mem != (expected_memory(instructions[index - 1], ptr_size) if index else []):
            return f"{path.name} line {index}: memory does not match json"
    return None


def check_frinet(path: Path, instructions: list[dict], gprs: list[str], ip: str, slide: int | None) -> str | None:
    """Each Frinet line holds its instruction with the registers and memory after execution."""
    actual_slide, lines = parse_text_trace(path)
    if not actual_slide or (slide is not None and actual_slide != slide):
        return f"{path.name}: unexpected slide {actual_slide}"
    if len(lines) != len(instructions):
        return f"{path.name} has {len(lines)} lines, json has {len(instructions)} instructions"
    state: dict[str, int] = {}
    for index, ((regs, mem), instruction) in enumerate(zip(lines, instructions)):
        if list(regs)[:1] != [ip] or regs[ip] != int(instruction["address"], 16) or set(regs) - set(gprs) - {ip}:
            return f"{path.name} line {index}: unexpected registers {list(regs)}"
        after = {name: value for name, value in regs.items() if name != ip}
        state.update(after)
        following = instructions[index + 1] if index + 1 < len(instructions) else None
        if following and following["thread"] == instruction["thread"]:
            if state != {name: int(following["regs"][name], 16) for name in gprs}:
                return f"{path.name} line {index}: registers after execution do not match json"
        elif after:
            return f"{path.name} line {index}: registers written without a following instruction"
        if mem != expected_memory(instruction, 8):
            return f"{path.name} line {index}: memory does not match json"
    return None


def main() -> int:
    args = parse_args()
    runtime_dir = Path(args.runtime_dir)

    try:
        document = json.loads((runtime_dir / "trace.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return fail(f"trace.json is not valid JSON: {exc}")

    instructions = document.get("instructions")
    if not instructions:
        return fail("trace.json has no instructions")
    ip = "rip" if document.get("arch") == "x64" else "eip"
    for expected_index, instruction in enumerate(instructions):
        if instruction.get("index") != expected_index:
            return fail(f"unexpected index at position {expected_index}")
        if not instruction.get("disasm") or not instruction.get("bytes"):
            return fail(f"instruction {expected_index} has no disassembly")
        if instruction["regs"].get(ip) != instruction["address"]:
            return fail(f"instruction {expected_index}: {ip} does not match address")

    with open(runtime_dir / "trace.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if len(rows) != len(instructions):
        return fail(f"csv has {len(rows)} rows, json has {len(instructions)} instructions")
    for row, instruction in zip(rows, instructions):
        if row["address"] != instruction["address"] or row["disasm"] != instruction["disasm"]:
            return fail(f"csv row {row['index']} does not match json")

    x64 = document.get("arch") == "x64"
    gprs = [name for name in instructions[0]["regs"] if name not in (ip, "rflags", "eflags")]
    checks = [lambda: check_tenet(runtime_dir / "trace.log", instructions, gprs, ip, 8 if x64 else 4)]
    if x64:
        checks.append(lambda: check_frinet(runtime_dir / "trace.tenet", instructions, gprs, ip, None))
        checks.append(lambda: check_frinet(runtime_dir / "trace.frinet.txt", instructions, gprs, ip, 0x140000000))
    for check in checks:
        try:
            error = check()
        except (OSError, ValueError) as exc:
            error = f"text trace is malformed: {exc}"
        if error:
            return fail(error)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
