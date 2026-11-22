#!/usr/bin/env python3
"""fathom_emu: run code from a minidump in an emulator, with Windows behind it.

Built on dumpulator (https://github.com/mrexodia/dumpulator, vendored in this folder): the memory,
modules and thread of a minidump are loaded in Unicorn, and the system calls the code makes are
implemented in Python (memory, files, registry, events...), as are exceptions (SEH, VEH). A function
of the process can be called with arguments, or the dumped thread run on.

    python fathom_emu.py info   app.dmp
    python fathom_emu.py call   app.dmp 0x140001000 0x1234 str:hello buf:256
    python fathom_emu.py call   app.dmp kernel32!lstrlenA wstr:x --trace out.trace64
    python fathom_emu.py run    app.dmp --end 0x140001234 --count 100000
    python fathom_emu.py script app.dmp decrypt.py [script arguments]
    python fathom_emu.py setup                     # pip install the requirements

Arguments of call: an integer (0x... or decimal), str:TEXT (an ANSI string allocated in the
emulator), wstr:TEXT (UTF-16), buf:SIZE (a zeroed buffer; its content is shown after the call),
bytes:HEX (a buffer with these bytes).

--trace FILE records the emulated instructions in x64dbg's trace format (.trace64 / .trace32): the
registers before each instruction and the memory it accessed. Fathom opens it in its Time Travel tab
and its trace view, and TraceExport converts it to JSON, CSV, Tenet or .ftt.

In a script, `dp` is the Dumpulator of the dump (dp.call, dp.read, dp.allocate, dp.regs,
@syscall...), `args` the script's arguments; see README.md.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

REQUIREMENTS = os.path.join(HERE, "requirements.txt")


def _missing_requirements() -> list[str]:
    missing = []
    for module in ("unicorn", "capstone", "pefile"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    return missing


def _load_dumpulator():
    missing = _missing_requirements()
    if missing:
        raise SystemExit(f"fathom_emu: missing Python packages: {', '.join(missing)}\n"
                         f"install them with: \"{sys.executable}\" -m pip install -r \"{REQUIREMENTS}\"\n"
                         f"(or the emu.setup command in Fathom)")
    import dumpulator
    return dumpulator


# ------------------------------------------------------------------------------ trace writer --

# REGDUMP of x64dbg (src/bridge/bridgemain.h): offsets of the members the emulator fills, and the
# number of pointer-sized words of a register block (the struct up to lastError.code)
_LAYOUTS = {
    True: dict(ptr=8, words=172, cip=128, segs=144, dr=160, mxcsr=316, xmm=320, ymm=576, flags=1088, mxcsr_fields=1328, nsse=16),
    False: dict(ptr=4, words=216, cip=32, segs=40, dr=52, mxcsr=184, xmm=192, ymm=320, flags=576, mxcsr_fields=816, nsse=8),
}

# Bits of EFLAGS in the FLAGS struct (c, p, a, z, s, t, i, d, o) and of MXCSR in MXCSRFIELDS
# (FZ, PM, UM, OM, ZM, IM, DM, DAZ, PE, UE, OE, ZE, DE, IE)
_EFLAGS_BITS = (0, 2, 4, 6, 7, 8, 9, 10, 11)
_MXCSR_BITS = (15, 12, 11, 10, 9, 7, 8, 6, 5, 4, 3, 2, 1, 0)

def _unicorn_library():
    """The ctypes library of unicorn (2.1: unicorn_py3.unicorn.uclib, 2.0: unicorn.unicorn._uc)."""
    for module, name in (("unicorn.unicorn_py3.unicorn", "uclib"), ("unicorn.unicorn", "_uc")):
        try:
            return getattr(__import__(module, fromlist=[name]), name)
        except (ImportError, AttributeError):
            continue
    return None


FULL_DUMP_INTERVAL = 512  # as x64dbg's recorder: all the registers every 512 instructions
MAX_MEMORY_ACCESSES = 32


class TraceWriter:
    """Records the instructions a Dumpulator executes to an x64dbg trace file (.trace64/.trace32).

    A block is written for each instruction when the next one starts: the registers before it
    (those that changed since the previous block, all of them every 512 instructions) and the
    memory it accessed, read before and after it ran, one pointer-sized word per entry. Instructions
    in the kernel half of the address space (dumpulator's exception return stub) and, for a 32-bit
    process, in 64-bit code are left out. System calls are implemented in Python: what they write to
    memory is not in the trace, only their result in the registers.
    """

    def __init__(self, dp, path: str, *, hash_value: int = 0, module_path: str = "", limit: int = 0):
        from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UcError
        from unicorn import x86_const as x86

        self._UcError = UcError
        self.dp = dp
        self.uc = dp._uc
        self.x64 = bool(dp._x64)
        self.layout = _LAYOUTS[self.x64]
        self.ptr = self.layout["ptr"]
        self.mask = (1 << (self.ptr * 8)) - 1
        self.user_limit = 0x800000000000 if self.x64 else 0x100000000
        self.limit = limit
        self.path = path
        self.count = 0
        # per thread: [address, opcode, registers, memory [[address, old], ...]], written when the
        # same thread starts its next instruction (as x64dbg's recorder does)
        self.pending = {}
        self.written = None  # the registers of the last block written
        self.written_thread = None  # the thread of the last block written
        self.closed = False

        gprs = ["RAX", "RCX", "RDX", "RBX", "RSP", "RBP", "RSI", "RDI"]
        if self.x64:
            gprs += ["R8", "R9", "R10", "R11", "R12", "R13", "R14", "R15"]
        names = gprs + ["RIP", "EFLAGS", "GS", "FS", "ES", "DS", "CS", "SS", "DR0", "DR1", "DR2", "DR3", "DR6", "DR7", "MXCSR"]
        self.ngpr = len(gprs)
        layout = self.layout
        nsse = layout["nsse"]
        p = "Q" if self.x64 else "I"
        self.size = layout["words"] * self.ptr
        self.gpr_format = struct.Struct(f"<{self.ngpr + 2}{p}")
        self.dr_format = struct.Struct(f"<6{p}")
        self.words_format = struct.Struct(f"<{layout['words']}{p}")
        self.word_format = struct.Struct("<" + p)
        # Words derived from EFLAGS (the FLAGS struct) are compared with the general registers
        flags = layout["flags"]
        self.flag_words = list(range(flags // self.ptr, (flags + 8) // self.ptr + 1))
        self.fast_words = list(range(self.ngpr + 2)) + self.flag_words
        self.flags_cache: dict = {}
        self.mxcsr_cache: dict = {}

        # The register image (REGDUMP up to lastError.code), read by one uc_reg_read_batch: the
        # scalar registers into 64-bit slots, the vector registers straight into the image
        self.state = ctypes.create_string_buffer(self.size)
        self.scalars = (ctypes.c_uint64 * len(names))()
        ids = [getattr(x86, "UC_X86_REG_" + name) for name in names]
        pointers = [ctypes.addressof(self.scalars) + 8 * i for i in range(len(names))]
        sizes = [8] * len(names)
        base = ctypes.addressof(self.state)
        ymm_ids = [getattr(x86, f"UC_X86_REG_YMM{i}", None) for i in range(nsse)]
        xmm_ids = [getattr(x86, f"UC_X86_REG_XMM{i}") for i in range(nsse)]
        try:
            if None in ymm_ids:
                raise ValueError("no YMM registers")
            self.uc.reg_read(ymm_ids[0])
            self.sse = "ymm"
        except (UcError, ValueError):
            self.sse = "xmm"
        for i in range(nsse):
            ids.append(xmm_ids[i])
            pointers.append(base + layout["xmm"] + 16 * i)
            sizes.append(16)
            if self.sse == "ymm":
                ids.append(ymm_ids[i])
                sizes.append(32)
            else:  # the low half of the YMM register
                ids.append(xmm_ids[i])
                sizes.append(16)
            pointers.append(base + layout["ymm"] + 32 * i)
        self.reg_count = len(ids)
        self.reg_ids = (ctypes.c_int * len(ids))(*ids)
        self.reg_pointers = (ctypes.c_void_p * len(ids))(*pointers)
        self.lib = _unicorn_library()
        self.handle = getattr(self.uc, "_uch", None)
        if self.lib is None or self.handle is None or not hasattr(self.lib, "uc_reg_read_batch"):
            self.lib = None
        self.slow_ids = list(zip(ids, pointers, sizes))

        self.file = open(path, "wb")
        header = json.dumps({
            "ver": 1,
            "arch": "x64" if self.x64 else "x86",
            "hashAlgorithm": "murmurhash",
            "hash": f"0x{hash_value:X}",
            "compression": "",
            "path": module_path,
            "emulator": "dumpulator",
        }, separators=(",", ":")).encode()
        self.file.write(b"TRAC" + struct.pack("<I", len(header)) + header)
        self.buffer = bytearray()

        self.hooks = [
            self.uc.hook_add(UC_HOOK_CODE, self._on_code),
            self.uc.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, self._on_memory),
        ]

    # -- registers
    def _read_batch(self):
        if self.lib is not None:
            status = self.lib.uc_reg_read_batch(self.handle, self.reg_ids, self.reg_pointers, self.reg_count)
            if status != 0:
                raise self._UcError(status)
            return
        # without the unicorn library's handle: one register at a time
        for reg, pointer, size in self.slow_ids:
            value = self.uc.reg_read(reg)
            if isinstance(value, tuple):
                value = 0
            ctypes.memmove(pointer, (value & ((1 << (8 * size)) - 1)).to_bytes(size, "little"), size)

    def _registers(self) -> tuple:
        self._read_batch()
        layout = self.layout
        state = self.state
        values = self.scalars[:]
        n = self.ngpr
        if self.x64:
            self.gpr_format.pack_into(state, 0, *values[:n + 2])
            self.dr_format.pack_into(state, layout["dr"], *values[n + 8:n + 14])
        else:
            self.gpr_format.pack_into(state, 0, *[v & 0xFFFFFFFF for v in values[:n + 2]])
            self.dr_format.pack_into(state, layout["dr"], *[v & 0xFFFFFFFF for v in values[n + 8:n + 14]])
        struct.pack_into("<6H", state, layout["segs"], *[v & 0xFFFF for v in values[n + 2:n + 8]])
        eflags = values[n + 1] & 0xFFF
        flags = self.flags_cache.get(eflags)
        if flags is None:
            flags = self.flags_cache[eflags] = bytes((eflags >> bit) & 1 for bit in _EFLAGS_BITS)
        offset = layout["flags"]
        state[offset:offset + 9] = flags
        mxcsr = values[n + 14] & 0xFFFFFFFF
        fields = self.mxcsr_cache.get(mxcsr)
        if fields is None:
            fields = self.mxcsr_cache[mxcsr] = bytes((mxcsr >> bit) & 1 for bit in _MXCSR_BITS) + struct.pack("<H", (mxcsr >> 13) & 3)
        offset = layout["mxcsr_fields"]
        state[offset:offset + 16] = fields
        struct.pack_into("<I", state, layout["mxcsr"], mxcsr)
        return self.words_format.unpack_from(state)

    def _read_word(self, address: int):
        """The pointer-sized word at address (zero-padded past the end of readable memory), None
        when its first byte cannot be read."""
        try:
            return int.from_bytes(bytes(self.uc.mem_read(address, self.ptr)), "little")
        except self._UcError:
            pass
        data = bytearray()
        for i in range(self.ptr):
            try:
                data += self.uc.mem_read(address + i, 1)
            except self._UcError:
                try:
                    data += self.dp.read(address + i, 1)  # a page dumpulator has not committed yet
                except Exception:
                    break
        if not data:
            return None
        return int.from_bytes(bytes(data), "little")

    # -- hooks
    def _on_code(self, uc, address, size, _user_data):
        if self.closed or (self.limit and self.count >= self.limit):
            return
        thread = self.dp.thread_id
        registers = None
        pending = self.pending.get(thread)
        if pending is not None:
            registers = self._registers()
            if address == pending[0] and registers == pending[2]:
                # The same instruction again without having executed: dumpulator restarts the
                # instruction that raised an exception to find its exact state
                pending[3] = []
                return
            self._write(pending, registers, thread)
            del self.pending[thread]
        if address >= self.user_limit:
            return
        if registers is None:
            registers = self._registers()
        try:
            opcode = bytes(uc.mem_read(address, min(size, 15)))
        except self._UcError:
            return
        self.pending[thread] = [address, opcode, registers, []]

    def _on_memory(self, uc, access, address, size, value, _user_data):
        pending = self.pending.get(self.dp.thread_id)
        if pending is None:
            return
        accesses = pending[3]
        start = address - (address % self.ptr) if size > self.ptr else address
        for word in range(start, address + size, self.ptr):
            if len(accesses) >= MAX_MEMORY_ACCESSES:
                return
            word &= self.mask
            if any(entry[0] == word for entry in accesses):
                continue
            value = self._read_word(word)
            if value is not None:  # not an access that faults (the instruction does not execute)
                accesses.append([word, value])

    # -- blocks
    def _write(self, block, after: tuple, thread: int):
        address, opcode, registers, accesses = block
        full = self.written is None or self.count % FULL_DUMP_INTERVAL == 0
        written = self.written
        if full:
            changed = range(len(registers))
        else:
            # the general registers and the flags one by one, the rest (vector, debug, segment
            # registers) only when it differs as a whole
            changed = [i for i in self.fast_words if registers[i] != written[i] or registers[i] != after[i]]
            first, last = self.ngpr + 2, self.flag_words[0]
            after_flags = self.flag_words[-1] + 1
            if (registers[first:last] != written[first:last] or registers[first:last] != after[first:last]
                    or registers[after_flags:] != written[after_flags:] or registers[after_flags:] != after[after_flags:]):
                changed = sorted(set(changed) | {i for i in range(len(registers))
                                                 if registers[i] != written[i] or registers[i] != after[i]})
        write_thread = full or thread != self.written_thread
        out = self.buffer
        out += bytes((0, len(changed), len(accesses), (0x80 if write_thread else 0) | len(opcode)))
        if write_thread:
            out += struct.pack("<I", thread)
        out += opcode
        last = -1
        for i in changed:
            out.append(i - last - 1)
            last = i
        pack = self.word_format.pack
        for i in changed:
            out += pack(registers[i])
        news = []
        for word, old in accesses:
            new = self._read_word(word)
            news.append((word, old, old if new is None else new))
        out += bytes(1 if old == new else 0 for _, old, new in news)
        for word, _, _ in news:
            out += pack(word)
        for _, old, _ in news:
            out += pack(old)
        for _, old, new in news:
            if old != new:
                out += pack(new)
        self.written = registers
        self.written_thread = thread
        self.count += 1
        if len(out) >= 1 << 20:
            self.file.write(out)
            out.clear()

    def close(self) -> int:
        """Write the last instruction (its state after: the current registers) and close the file.
        Returns the number of instructions recorded."""
        if self.closed:
            return self.count
        # the last instruction of each thread: its state after is the current one for the running
        # thread; the other threads stopped in a system call, their registers did not change since
        for thread, pending in list(self.pending.items()):
            if self.limit and self.count >= self.limit:
                break
            after = self._registers() if thread == self.dp.thread_id else pending[2]
            self._write(pending, after, thread)
        self.pending = {}
        self.closed = True
        for hook in self.hooks:
            try:
                self.uc.hook_del(hook)
            except Exception:
                pass
        self.file.write(self.buffer)
        self.buffer.clear()
        self.file.close()
        return self.count


# ------------------------------------------------------------------------------ helpers --

def parse_int(text: str) -> int:
    text = text.strip()
    return int(text, 0) if not text.lower().startswith("0x") else int(text, 16)


def resolve_address(dp, text: str) -> int:
    """0x..., decimal, or module!export / module:export / module.export (module name with or
    without its extension)."""
    try:
        return parse_int(text)
    except ValueError:
        pass
    for separator in ("!", ":", "."):
        if separator not in text:
            continue
        module_name, export = text.rsplit(separator, 1)
        module = dp.modules.find(module_name)
        if module is None:
            for extension in (".dll", ".exe", ".sys"):
                module = dp.modules.find(module_name + extension)
                if module is not None:
                    break
        if module is None:
            continue
        found = module.find_export(int(export, 0) if export.isdigit() else export)
        if found is not None:
            if getattr(found, "forward", None):  # kernel32!X forwarded to another DLL
                found = dp.modules.resolve_export(*found.forward)
            return found.address
    raise SystemExit(f"fathom_emu: cannot resolve \"{text}\" (an address, or module!export)")


def build_arguments(dp, texts: list[str]):
    """The integer values of call arguments, and the buffers allocated for them."""
    values, buffers = [], []
    for index, text in enumerate(texts):
        kind, _, rest = text.partition(":")
        kind = kind.lower()
        if kind in ("str", "wstr", "buf", "bytes") and _:
            if kind == "str":
                data = rest.encode("mbcs" if os.name == "nt" else "latin-1", "replace") + b"\0"
            elif kind == "wstr":
                data = rest.encode("utf-16-le") + b"\0\0"
            elif kind == "buf":
                data = bytes(parse_int(rest))
            else:
                data = bytes.fromhex(rest)
            address = dp.allocate(max(len(data), 1))
            dp.write(address, data)
            values.append(address)
            buffers.append(dict(index=index, kind=kind, address=address, size=len(data), before=data))
        else:
            values.append(parse_int(text) & dp.addr_mask)
    return values, buffers


def describe_buffer(dp, buffer: dict) -> dict:
    data = bytes(dp.read(buffer["address"], buffer["size"]))
    text = data.decode("utf-16-le", "replace") if buffer["kind"] == "wstr" else data.decode("latin-1")
    end = text.find("\0")
    return dict(index=buffer["index"], kind=buffer["kind"], address=f"0x{buffer['address']:X}", size=buffer["size"],
                changed=data != buffer["before"], hex=data.hex(), text=text if end < 0 else text[:end])


def trace_path(path: str, x64: bool) -> str:
    root, extension = os.path.splitext(path)
    wanted = ".trace64" if x64 else ".trace32"
    if extension.lower() in (".trace64", ".trace32", ""):
        return root + wanted
    return path


class Session:
    """The dump loaded in dumpulator, with the options common to the actions."""

    def __init__(self, args):
        dumpulator = _load_dumpulator()
        self.args = args
        self.result: dict = dict(ok=False, dump=os.path.abspath(args.dump))
        started = time.perf_counter()
        thread_id = parse_int(args.thread) if args.thread else None
        self.dp = dumpulator.Dumpulator(args.dump, quiet=True, thread_id=thread_id, trace=bool(args.text_trace))
        self.dp._quiet = not args.syscalls  # the system calls and exceptions in the output, not the memory map
        self.dp.strict = bool(getattr(args, "strict", False))
        configure(self.dp, cpus=getattr(args, "cpus", None), seed=getattr(args, "seed", None),
                  network=getattr(args, "network", None), http_body=getattr(args, "http_body", None),
                  child_limit=getattr(args, "child_limit", None), screenshots=getattr(args, "screenshots", None))
        self.files_out = getattr(args, "files_out", None)
        self.result.update(arch="x64" if self.dp._x64 else "x86", thread=self.dp.thread_id,
                           load_seconds=round(time.perf_counter() - started, 3))
        self.writer = None
        if args.trace:
            path = trace_path(args.trace, self.dp._x64)
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self.writer = TraceWriter(self.dp, path, hash_value=parse_int(args.hash) if args.hash else 0,
                                      module_path=args.module_path or self._main_module_path(), limit=args.trace_limit)
            self.result["trace"] = os.path.abspath(path)

    def _main_module_path(self) -> str:
        module = self.dp.modules.find(self.dp.modules.main) if hasattr(self.dp.modules, "main") else None
        return module.path if module is not None else ""

    def finish(self, started: float):
        dp = self.dp
        if self.writer is not None:
            self.result["instructions"] = self.writer.close()
        self.result.update(cip=f"0x{dp.regs.cip:X}", exit_code=dp.exit_code,
                           emulation_seconds=round(time.perf_counter() - started, 3))
        if dp.trace:
            dp.trace.close()
            self.result["text_trace"] = os.path.abspath(dp.trace.name)
        scheduler = getattr(dp, "scheduler", None)
        if scheduler is not None:
            self.result["threads"] = [dict(tid=t.tid, state=t.state, exit_code=t.exit_code)
                                      for t in scheduler.threads.values() if t.state != "frozen"]
            if scheduler.stop_reason:
                self.result["stop_reason"] = scheduler.stop_reason
        missing = getattr(dp, "_fathom_unimplemented", None)
        if missing:
            self.result["not_implemented"] = dict(missing)
        console = getattr(dp, "_fathom_console", None)
        if console:
            self.result["console"] = "".join(console)
        self.result.update(world(dp, self.files_out))


def configure(dp, cpus=None, seed=None, network=None, http_body=None, child_limit=None, screenshots=None):
    """The options of the emulated machine: processors, interleaving seed, network, child processes,
    images of the windows."""
    import random
    scheduler = dp.scheduler
    if cpus:
        scheduler.cpus = max(1, int(cpus))
    if seed is not None:
        scheduler.rng = random.Random(int(seed))
        if getattr(dp, "world", None) is not None:
            dp.world.rng = random.Random(int(seed) + 1)
    net = getattr(dp, "network", None)
    if net is not None:
        if network:
            net.mode = network
        if http_body:
            with open(http_body, "rb") as f:
                net.body = f.read()
    processes = getattr(dp, "processes", None)
    if processes is not None and child_limit:
        processes.instruction_limit = int(child_limit)
    gui = getattr(dp, "gui", None)
    if gui is not None and screenshots:
        gui.drawing.directory = os.path.abspath(screenshots)


def world(dp, files_out=None) -> dict:
    """What the program did outside of its process: network, windows, child processes, registry,
    files, shared objects, COM / RPC / services."""
    result = {}
    net = getattr(dp, "network", None)
    if net is not None and net.events:
        result["network"] = list(net.events)
    shared_world = getattr(dp, "world", None)
    everyone = [dp] + [c.dp for c in (shared_world.children if shared_world is not None else []) if c.dp is not None]
    shots = []
    for process in everyone:
        gui = getattr(process, "gui", None)
        if gui is not None and gui.drawing.directory:
            try:
                shots += gui.save_screenshots()
            except Exception as e:
                print(f"images of the windows: {type(e).__name__}: {e}")
    gui = getattr(dp, "gui", None)
    if gui is not None and gui.events:
        result["gui"] = list(gui.events)
    if shots:
        result["screenshots"] = sorted(set(shots))
    processes = getattr(dp, "processes", None)
    if processes is not None and processes.children:
        result["processes"] = [child.summary() for child in processes.children]
    com = getattr(dp, "com", None)
    if com is not None and com.events:
        result["com"] = list(com.events)
    holder = shared_world if shared_world is not None else dp
    registry = getattr(holder, "registry", None)
    if registry is not None and registry.events:
        result["registry"] = list(registry.events)
    filesystem = getattr(holder, "filesystem", None)
    if filesystem is not None:
        changes = [e for e in filesystem.events if e["kind"] != "write"]
        written = filesystem.written_files()
        if changes or written:
            result["files"] = changes + [dict(kind="written", path=s.path, size=len(s.data)) for s in written]
        if files_out and written:
            os.makedirs(files_out, exist_ok=True)
            saved = []
            for state in written:
                name = state.path.replace(":", "").replace("\\", "_").replace("\\", "_").strip("_")
                target = os.path.join(files_out, name)
                with open(target, "wb") as f:
                    f.write(bytes(state.data))
                saved.append(target)
            result["files_saved"] = saved
    shared = getattr(holder, "shared", None)
    if shared is not None and shared.events:
        result["shared_objects"] = list(shared.events)
    return result


def _emulate(session: Session, function):
    """Run function() (the emulation); an exception stops it, but the trace is still closed and
    the result written. Returns (value of function or None, error message or None)."""
    started = time.perf_counter()
    value, error = None, None
    try:
        value = function()
    except NotImplementedError as e:
        import traceback
        frames = traceback.extract_tb(e.__traceback__)
        where = frames[-1] if frames else None
        if where is not None and where.name.startswith(("Zw", "Nt")):
            error = f"stopped by a system call dumpulator does not implement: {where.name} (see --syscalls)"
        else:
            traceback.print_exc()
            error = f"stopped: not implemented in the emulator ({e or 'see above'})"
    except Exception as e:  # an error of the emulated code or of the emulator
        import traceback
        traceback.print_exc()
        error = f"{type(e).__name__}: {e}"
    session.finish(started)
    if error:
        session.result["error"] = error
        print(f"the emulation {error}" if error.startswith("stopped") else f"the emulation failed: {error}")
    return value, error


def _print_result(result: dict):
    if result.get("return") is not None:
        print(f"return value: {result['return']}")
    for buffer in result.get("buffers", []):
        state = "changed" if buffer["changed"] else "unchanged"
        print(f"argument {buffer['index'] + 1} ({buffer['kind']} at {buffer['address']}, {buffer['size']} bytes, {state}): {buffer['text']!r}")
        if buffer["kind"] in ("buf", "bytes"):
            print(f"  hex: {buffer['hex'][:512]}{'...' if len(buffer['hex']) > 512 else ''}")
    threads = result.get("threads", [])
    if len(threads) > 1:
        print(f"threads: " + ", ".join(f"{t['tid']} {t['state']}" + (f" ({t['exit_code']:#x})" if t['exit_code'] is not None else "")
                                      for t in threads))
    missing = result.get("not_implemented")
    if missing:
        print("not in the emulator (failed in the program): " + ", ".join(f"{name} x{count}" for name, count in missing.items()))
    if result.get("stop_reason"):
        print(f"stopped: {result['stop_reason']}")
    network = result.get("network", [])
    if network:
        hosts = sorted({str(e.get("host") or e.get("url") or e.get("name")) for e in network
                        if e["kind"] in ("connect", "http_request", "dns")})
        print(f"network: {len(network)} operations" + (f" ({', '.join(hosts[:8])}{', ...' if len(hosts) > 8 else ''})" if hosts else ""))
    gui = result.get("gui", [])
    if gui:
        print(f"windows: {len(gui)} operations")
    for path in result.get("screenshots", []):
        print(f"image of a window: {path}")
    if result.get("registry"):
        changes = result["registry"]
        print(f"registry: {len(changes)} changes (in the emulator): " + ", ".join(
            sorted({e["key"].split("\\")[-1] for e in changes})[:6]))
    if result.get("files"):
        print(f"files: {len(result['files'])} changes (in the emulator)" + (
            f", saved in {os.path.dirname(result['files_saved'][0])}" if result.get("files_saved") else ""))
    if result.get("shared_objects"):
        print(f"objects shared between processes: {len(result['shared_objects'])}")
    for event in result.get("com", []):
        if event["kind"] in ("com_create", "com_out_of_process", "rpc_call", "create_service", "start_service"):
            detail = {k: v for k, v in event.items() if k not in ("kind",)}
            print(f"{event['kind']}: " + ", ".join(f"{k}={v}" for k, v in detail.items()))
    for child in result.get("processes", []):
        state = f"exit code {child['exit_code']:#x}" if child.get("exit_code") is not None else child["state"]
        print(f"child process {child['pid']}: {child['command_line']} ({state})" + (f", {child['note']}" if child.get("note") else ""))
    if result.get("exit_code") is not None:
        print(f"the process exited with code {result['exit_code']:#x}")
    if "trace" in result:
        print(f"trace: {result['trace']} ({result.get('instructions', 0)} instructions)")
    print(f"emulation: {result.get('emulation_seconds', 0)} s (dump loaded in {result.get('load_seconds', 0)} s), cip = {result.get('cip')}")


# ------------------------------------------------------------------------------ actions --

def action_info(args) -> dict:
    dumpulator = _load_dumpulator()
    dp = dumpulator.Dumpulator(args.dump, quiet=True)
    minidump = dp._minidump
    print(f"dump: {os.path.abspath(args.dump)}")
    print(f"architecture: {'x64' if dp._x64 else 'x86 (WoW64)'}, process {dp.process_id}, emulated thread {dp.thread_id}")
    print(f"registers: cip = 0x{dp.regs.cip:X}, csp = 0x{dp.regs.csp:X}")
    print("threads:")
    for thread in minidump.threads.threads:
        context = thread.ContextObject
        ip = getattr(context, "Rip", None)
        if ip is None:
            ip = getattr(context, "Eip", 0)
        marker = "*" if thread.ThreadId == dp.thread_id else " "
        print(f" {marker} {thread.ThreadId:6}  ip 0x{ip:X}")
    print("modules:")
    modules = sorted(dp.modules, key=lambda m: m.base)
    for module in modules:
        print(f"   0x{module.base:016X}  0x{module.size:08X}  {module.path}")
    return dict(ok=True, dump=os.path.abspath(args.dump), arch="x64" if dp._x64 else "x86", thread=dp.thread_id,
                cip=f"0x{dp.regs.cip:X}", modules=len(modules), threads=len(minidump.threads.threads))


def action_call(args) -> dict:
    session = Session(args)
    dp = session.dp
    address = resolve_address(dp, args.address)
    values, buffers = build_arguments(dp, args.arguments)
    print(f"call 0x{address:X}({', '.join(hex(v) for v in values)}) in thread {dp.thread_id}")
    value, error = _emulate(session, lambda: dp.call(address, values, count=args.count))
    result = session.result
    result.update(ok=error is None and dp.kill_exception is None, address=f"0x{address:X}",
                  arguments=[f"0x{v:X}" for v in values], buffers=[describe_buffer(dp, b) for b in buffers])
    if value is None:
        value = dp.regs.cax
    result["return"] = f"0x{value & dp.addr_mask:X}"
    returned = (dp.regs.cip & dp.addr_mask) == 0x5000  # USER_CAVE: the return address pushed by call()
    result["returned"] = returned
    if not returned and dp.exit_code is None:
        print(f"the function did not return (stopped at 0x{dp.regs.cip:X}{', instruction limit' if args.count else ''})")
    _print_result(result)
    return result


def action_run(args) -> dict:
    session = Session(args)
    dp = session.dp
    begin = resolve_address(dp, args.begin) if args.begin else dp.regs.cip
    end = resolve_address(dp, args.end) if args.end else 0xFFFFFFFFFFFFFFFF
    print(f"run from 0x{begin:X} in thread {dp.thread_id}" + (f" to 0x{end:X}" if args.end else "")
          + (f", {args.count} instructions at most" if args.count else ""))
    def run():
        dp.start(begin, end=end, count=args.count)
        world = getattr(dp, "world", None)
        if world is not None and dp.exit_code is not None and world.children:
            # the program exited: its child processes go on until they end too
            world.finish()
    _, error = _emulate(session, run)
    result = session.result
    result.update(ok=error is None and dp.kill_exception is None, begin=f"0x{begin:X}")
    if args.end:
        result["reached_end"] = (dp.regs.cip & dp.addr_mask) == (end & dp.addr_mask)
    _print_result(result)
    return result


def action_script(args) -> dict:
    session = Session(args)
    dp = session.dp
    import dumpulator
    from dumpulator import native
    script = os.path.abspath(args.script)
    namespace = {"__name__": "__main__", "__file__": script, "dp": dp, "args": args.script_args,
                 "Dumpulator": dumpulator.Dumpulator, "syscall": dumpulator.syscall, "native": native,
                 "result": session.result}
    with open(script, encoding="utf-8") as f:
        code = compile(f.read(), script, "exec")
    sys.path.insert(0, os.path.dirname(script))
    _, error = _emulate(session, lambda: exec(code, namespace))
    value = session.result.get("return")
    if isinstance(value, int):  # result["return"] = value: $result in Fathom
        session.result["return"] = f"0x{value & dp.addr_mask:X}"
    session.result["ok"] = error is None
    if "trace" in session.result:
        print(f"trace: {session.result['trace']} ({session.result.get('instructions', 0)} instructions)")
    return session.result


def action_setup(args) -> dict:
    import subprocess
    command = [sys.executable, "-m", "pip", "install", "-r", REQUIREMENTS]
    if not args.system and sys.prefix == sys.base_prefix:  # not in a virtual environment
        command.insert(4, "--user")
    print(" ".join(f'"{c}"' if " " in c else c for c in command))
    code = subprocess.call(command)
    missing = _missing_requirements() if code == 0 else []
    if code == 0 and not missing:
        print("the emulator is ready")
    return dict(ok=code == 0 and not missing, exit_code=code)


# ------------------------------------------------------------------------------ main --

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="fathom_emu", description="Emulate code from a minidump (dumpulator).",
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    parser.add_argument("--result", metavar="FILE", help="write the result as JSON to this file")
    actions = parser.add_subparsers(dest="action", required=True)

    def result_option(sub):
        # accepted after the action too; SUPPRESS keeps the value given before it
        sub.add_argument("--result", metavar="FILE", default=argparse.SUPPRESS, help="write the result as JSON to this file")

    def emulation_options(sub):
        result_option(sub)
        sub.add_argument("dump", help="minidump (.dmp) with full memory, e.g. from Fathom's minidump command")
        sub.add_argument("--thread", help="thread to emulate (default: the thread of the dump's exception, else the first)")
        sub.add_argument("--count", type=lambda t: parse_int(t), default=0, help="stop after this many instructions (0: no limit)")
        sub.add_argument("--trace", metavar="FILE", help="record the instructions to an x64dbg trace (.trace64/.trace32)")
        sub.add_argument("--trace-limit", type=lambda t: parse_int(t), default=0, help="record at most this many instructions")
        sub.add_argument("--text-trace", action="store_true", help="also write dumpulator's text trace (<dump>.trace)")
        sub.add_argument("--syscalls", action="store_true", help="show the system calls and exceptions")
        sub.add_argument("--strict", action="store_true",
                         help="stop at a system call or an instruction the emulator lacks (default: it fails in the program)")
        sub.add_argument("--cpus", type=int, help="processors of the emulated machine (default: those of the dumped machine)")
        sub.add_argument("--seed", type=int, help="seed of the interleaving of the threads (default 1: the same run every time)")
        sub.add_argument("--network", choices=("simulated", "real"),
                         help="simulated (default): names resolve to 192.0.2.x, connections succeed, HTTP answers 200; "
                              "real: through the host's network (only for a program you trust)")
        sub.add_argument("--http-body", metavar="FILE", help="the body of the simulated HTTP responses")
        sub.add_argument("--child-limit", type=lambda t: parse_int(t),
                         help="instructions a child process runs at most (default 50000000)")
        sub.add_argument("--screenshots", metavar="DIR", help="save images (PNG) of the windows of the program in DIR")
        sub.add_argument("--files-out", metavar="DIR",
                         help="save the files the program wrote (they stay in the emulator otherwise) in DIR")
        sub.add_argument("--hash", help=argparse.SUPPRESS)  # module hash of the debugged process (trace header)
        sub.add_argument("--module-path", help=argparse.SUPPRESS)  # main module path (trace header)

    sub = actions.add_parser("info", help="threads and modules of a dump")
    result_option(sub)
    sub.add_argument("dump")
    sub.set_defaults(handler=action_info)

    sub = actions.add_parser("call", help="call a function: call DUMP ADDRESS [ARG ...]")
    emulation_options(sub)
    sub.add_argument("address", help="0x..., or module!export")
    sub.add_argument("arguments", nargs="*", help="integers, str:TEXT, wstr:TEXT, buf:SIZE, bytes:HEX")
    sub.set_defaults(handler=action_call)

    sub = actions.add_parser("run", help="run the dumped thread on")
    emulation_options(sub)
    sub.add_argument("--begin", help="start address (default: the thread's instruction pointer)")
    sub.add_argument("--end", help="stop when this address is reached")
    sub.set_defaults(handler=action_run)

    sub = actions.add_parser("script", help="run a Python script with dp (the Dumpulator of the dump)")
    emulation_options(sub)
    sub.add_argument("script")
    sub.add_argument("script_args", nargs=argparse.REMAINDER)
    sub.set_defaults(handler=action_script)

    sub = actions.add_parser("setup", help="install the Python packages the emulator needs (pip)")
    result_option(sub)
    sub.add_argument("--system", action="store_true", help="install for every user, not with --user")
    sub.set_defaults(handler=action_setup)

    args = parser.parse_args(argv)
    result: dict = dict(ok=False)
    code = 1
    try:
        result = args.handler(args)
        code = 0 if result.get("ok") else 1
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            result = dict(ok=False, error=e.code)
        else:
            raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        result = dict(ok=False, error=f"{type(e).__name__}: {e}")
    finally:
        if args.result:
            try:
                # serialized first: a value json cannot write becomes text instead of a cut file
                text = json.dumps(result, indent=1, default=str)
            except Exception as e:
                print(f"the result could not be written as JSON: {type(e).__name__}: {e}")
                text = json.dumps(dict(ok=False, error=f"result not serializable: {e}"))
            with open(args.result, "w", encoding="utf-8") as f:
                f.write(text)
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
