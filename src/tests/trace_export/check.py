from __future__ import annotations

import argparse
import csv
import json
import struct
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


def lz4_block_decompress(src: bytes, size: int) -> bytes:
    """Decompress an LZ4 block (the format of the .ftt chunks), independently of the C++ reader."""
    out = bytearray()
    i = 0
    while i < len(src):
        token = src[i]
        i += 1
        literal = token >> 4
        if literal == 15:
            while True:
                b = src[i]
                i += 1
                literal += b
                if b != 255:
                    break
        out += src[i:i + literal]
        i += literal
        if i >= len(src):
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        match = token & 15
        if match == 15:
            while True:
                b = src[i]
                i += 1
                match += b
                if b != 255:
                    break
        match += 4
        start = len(out) - offset
        for k in range(match):
            out.append(out[start + k])
    if len(out) != size:
        raise ValueError(f"lz4: {len(out)} bytes instead of {size}")
    return bytes(out)


def ftt_registers(ptr_size: int) -> list[tuple[str, int]]:
    """The .ftt register catalog (docs/developers/ftt.md): (name, size), the id is the position."""
    if ptr_size == 8:
        regs = [(n, 8) for n in "rax rcx rdx rbx rsp rbp rsi rdi r8 r9 r10 r11 r12 r13 r14 r15 rip rflags".split()]
        nxmm = 16
    else:
        regs = [(n, 4) for n in "eax ecx edx ebx esp ebp esi edi eip eflags".split()]
        nxmm = 8
    regs += [(f"st{i}", 10) for i in range(8)] + [("fpcw", 2), ("fpsw", 2), ("fptw", 2)]
    regs += [(f"xmm{i}", 16) for i in range(nxmm)] + [("mxcsr", 4)]
    regs += [(f"ymm{i}h", 16) for i in range(nxmm)]
    return regs


def read_ftt(path: Path) -> dict:
    """Parse a .ftt file: header, metadata, chunks, index; decode every instruction with the full
    register state of its thread and the memory snapshots."""
    data = path.read_bytes()
    magic, version, ptr_size, levels, snapshot_interval, meta_size, _ = struct.unpack_from("<4sHHIIII", data, 0)
    if magic != b"FTT1" or version != 1:
        raise ValueError("bad header")
    meta = json.loads(data[24:24 + meta_size])
    index_offset, trailer = struct.unpack_from("<Q4s", data, len(data) - 16)
    if trailer != b"FTTX":
        raise ValueError("bad trailer")
    catalog = ftt_registers(ptr_size)

    def chunk(offset: int) -> tuple[tuple, bytes]:
        header = struct.unpack_from("<4sBBHIIQQQII", data, offset)
        cmagic, kind, compressed, _, thread, count, first, last, local, raw_size, stored = header
        if cmagic != b"FCHK":
            raise ValueError(f"bad chunk at {offset:#x}")
        body = data[offset + 48:offset + 48 + stored]
        raw = lz4_block_decompress(body, raw_size) if compressed else body
        return header, raw

    (_, kind, *_), raw = chunk(index_offset)
    if kind != 3:
        raise ValueError("bad index")
    count = struct.unpack_from("<I", raw, 0)[0]
    entries = [struct.unpack_from("<QBIIQQQ", raw, 4 + 41 * i) for i in range(count)]
    pos = 4 + 41 * count
    nthreads = struct.unpack_from("<I", raw, pos)[0]
    threads = [struct.unpack_from("<IQQQ", raw, pos + 4 + 28 * i) for i in range(nthreads)]
    total = struct.unpack_from("<Q", raw, pos + 4 + 28 * nthreads)[0]

    instructions = {}
    snapshots = []
    for offset, kind, thread, count, first, last, local in entries:
        header, raw = chunk(offset)
        if kind == 2:
            pages = {}
            for i in range(count):
                base = struct.unpack_from("<Q", raw, i * (8 + 512 + 4096))[0]
                known = raw[i * 4616 + 8:i * 4616 + 520]
                content = raw[i * 4616 + 520:i * 4616 + 4616]
                pages[base] = (known, content)
            snapshots.append((first, pages))
            continue
        if kind != 1:
            continue  # other chunks (4: CPU time samples) are skipped, like readers that do not know them
        regs = {}
        nkey = struct.unpack_from("<H", raw, 0)[0]
        p = 2
        for _ in range(nkey):
            rid = raw[p]
            regs[catalog[rid][0]] = raw[p + 1:p + 1 + catalog[rid][1]]
            p += 1 + catalog[rid][1]
        seq = first
        switch = None
        while p < len(raw):
            tag = raw[p]
            p += 1
            if tag == 2:
                switch = struct.unpack_from("<IQ", raw, p)
                p += 12
                continue
            if tag != 1:
                raise ValueError(f"unknown event {tag}")
            delta, shift = 0, 0
            while True:
                b = raw[p]
                p += 1
                delta |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
            seq += delta
            opsize = raw[p]
            opcode = raw[p + 1:p + 1 + opsize]
            p += 1 + opsize
            nchanged = raw[p]
            p += 1
            for _ in range(nchanged):
                rid = raw[p]
                regs[catalog[rid][0]] = raw[p + 1:p + 1 + catalog[rid][1]]
                p += 1 + catalog[rid][1]
            mem = []
            nmem = raw[p]
            p += 1
            for _ in range(nmem):
                flags, address, size = struct.unpack_from("<BQB", raw, p)
                p += 10
                old = None
                if not flags & 2:
                    old = raw[p:p + size]
                    p += size
                new = None
                if flags & 1:
                    new = raw[p:p + size]
                    p += size
                mem.append((address, size, old, new))
            instructions[seq] = {"thread": thread, "opcode": opcode, "regs": dict(regs), "mem": mem, "switch": switch}
            switch = None
    return {"ptr_size": ptr_size, "levels": levels, "meta": meta, "threads": threads, "total": total,
            "instructions": instructions, "snapshots": snapshots, "catalog": catalog}


def check_ftt(path: Path, instructions: list[dict], ip: str, all_registers: bool) -> str | None:
    """The .ftt holds the same instructions as the json export, in per-thread chunks, and its memory
    snapshots match the memory accesses replayed up to each snapshot."""
    ftt = read_ftt(path)
    if ftt["total"] != len(instructions) or sorted(ftt["instructions"]) != list(range(len(instructions))):
        return f"{path.name}: {len(ftt['instructions'])} instructions, json has {len(instructions)}"
    if sum(t[1] for t in ftt["threads"]) != len(instructions):
        return f"{path.name}: per-thread counts do not add up"
    ptr = ftt["ptr_size"]
    for seq, instruction in enumerate(instructions):
        entry = ftt["instructions"][seq]
        if entry["thread"] != instruction["thread"] or entry["opcode"].hex(" ").upper() != instruction["bytes"]:
            return f"{path.name} instruction {seq}: thread or opcode differs"
        for name, value in instruction["regs"].items():
            if int.from_bytes(entry["regs"][name], "little") != int(value, 16):
                return f"{path.name} instruction {seq}: {name} differs"
        if (entry["switch"] is not None) != ("switch" in instruction):
            return f"{path.name} instruction {seq}: context switch marker differs"
        if len(entry["mem"]) != len(instruction["mem"]):
            return f"{path.name} instruction {seq}: memory access count differs"
        for (address, size, old, new), access in zip(entry["mem"], instruction["mem"]):
            if address != int(access["address"], 16) or size != ptr:
                return f"{path.name} instruction {seq}: memory access differs"
            if old is not None and int.from_bytes(old, "little") != int(access["old"], 16):
                return f"{path.name} instruction {seq}: old memory value differs"
            written = int(access["old"], 16) != int(access["new"], 16)
            if (new is not None) and int.from_bytes(new, "little") != int(access["new"], 16):
                return f"{path.name} instruction {seq}: new memory value differs"
            if written and new is None:
                return f"{path.name} instruction {seq}: write without new value"
    if all_registers:
        names = {name for name, _ in ftt["catalog"]}
        if any(set(e["regs"]) != names for e in ftt["instructions"].values()):
            return f"{path.name}: not every register is stored"
    # memory snapshots: each known byte equals the memory replayed from the accesses before it
    memory: dict[int, int] = {}
    replayed = 0
    for seq, pages in sorted(ftt["snapshots"], key=lambda s: s[0]):
        for instruction in instructions[replayed:seq]:
            for access in instruction["mem"]:
                address = int(access["address"], 16)
                new = int(access["new"], 16).to_bytes(ptr, "little")
                for k in range(ptr):
                    memory[address + k] = new[k]
        replayed = seq
        for base, (known, content) in pages.items():
            for offset in range(4096):
                if known[offset // 8] >> (offset % 8) & 1 and memory.get(base + offset) != content[offset]:
                    return f"{path.name}: snapshot before {seq} differs at {base + offset:#x}"
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

    # .ftt: read back by an independent parser, exported again identically
    for name, all_registers in (("trace.ftt", False), ("trace.all.ftt", True)):
        try:
            error = check_ftt(runtime_dir / name, instructions, ip, all_registers)
        except (OSError, ValueError, IndexError, struct.error) as exc:
            error = f"{name} is malformed: {exc}"
        if error:
            return fail(error)
    if read_ftt(runtime_dir / "trace.all.ftt")["snapshots"] == []:
        return fail("trace.all.ftt has no memory snapshot")
    for original, again in (("trace.json", "trace.ftt.json"), ("trace.log", "trace.ftt.log")):
        if (runtime_dir / original).read_bytes() != (runtime_dir / again).read_bytes():
            return fail(f"{again} differs from {original}")

    # extended registers: the general purpose ones unchanged, the others present
    extended = json.loads((runtime_dir / "trace.regs.json").read_text(encoding="utf-8"))["instructions"]
    for full, instruction in zip(extended, instructions):
        if any(full["regs"][name] != value for name, value in instruction["regs"].items()):
            return fail(f"trace.regs.json instruction {full['index']}: general purpose registers differ")
        if not {"st0", "fpcw", "xmm0", "mxcsr", "ymm0h"} <= set(full["regs"]):
            return fail("trace.regs.json lacks the extended registers")
    with open(runtime_dir / "trace.sse.csv", newline="", encoding="utf-8") as f:
        header = next(csv.reader(f))
    if "xmm0" not in header or "st0" in header or header[-1] != "seq":
        return fail(f"trace.sse.csv has unexpected columns {header}")

    # one thread's timeline: the instructions of that thread, in order, with their global numbers
    single = json.loads((runtime_dir / "trace.thread.json").read_text(encoding="utf-8"))
    tid = single.get("thread")
    expected = [i for i in instructions if i["thread"] == tid]
    if [(i["seq"], i["address"], i["regs"]) for i in single["instructions"]] != [(i["seq"], i["address"], i["regs"]) for i in expected]:
        return fail("trace.thread.json is not the timeline of its thread")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
