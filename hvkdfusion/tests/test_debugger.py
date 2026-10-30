"""Tests of wineye's debugger layer, without any real target.

- x86-64 disassembler (WinDbg syntax) on known vectors;
- CONTEXT record round trip, MSVC name undecoration, bugcheck names;
- private PDB reader on tests/data/fixture.pdb (built from fixture.c): publics,
  procedures, source lines, parameters / locals, struct layouts;
- x64 stack unwinding (.pdata / UNWIND_INFO) on a synthetic process that maps
  tests/data/fixture.exe and a hand-built stack entry -> outer -> inner, then k, dv,
  .frame, ln, u, uf through the REPL;
- native crash-dump reader on a synthetic PAGEDU64 full dump (header, runs, 4-level
  page tables with 4 KB / 2 MB pages).

Exits non-zero on failure.
"""
import contextlib
import io
import os
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.pop("WINEYE_SYMPATH", None)
os.environ.pop("_NT_SYMBOL_PATH_LOCAL", None)
import wineye  # noqa: E402

DATA = HERE / "data"
_failures = 0


def check(name, cond):
    global _failures
    print(("  OK  " if cond else "  FAIL") + "  " + name)
    if not cond:
        _failures += 1


def output(fn, *a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a)
    return buf.getvalue()


# ----------------------------- disassembler -----------------------------
print("== disassembler ==")
dis = wineye.X64Disassembler()
VECTORS = [
    ("48894c2408", "mov     qword ptr [rsp+8],rcx"),
    ("4883ec30", "sub     rsp,30h"),
    ("9c", "pushfq"),
    ("fa", "cli"),
    ("65488b0c2520000000", "mov     rcx,qword ptr gs:[20h]"),
    ("488b89c0890000", "mov     rcx,qword ptr [rcx+89C0h]"),
    ("4881c100010000", "add     rcx,100h"),
    ("33c9", "xor     ecx,ecx"),
    ("4533c9", "xor     r9d,r9d"),
    ("4863c3", "movsxd  rax,ebx"),
    ("0fbe440408", "movsx   eax,byte ptr [rsp+rax+8]"),
    ("6b44244003", "imul    eax,dword ptr [rsp+40h],3"),
    ("48b88877665544332211", "mov     rax,1122334455667788h"),
    ("c644040877", "mov     byte ptr [rsp+rax+8],77h"),
    ("4821542420", "and     qword ptr [rsp+20h],rdx"),
    ("488d8f00010000", "lea     rcx,[rdi+100h]"),
    ("5b", "pop     rbx"),
    ("c3", "ret"),
    ("cc", "int     3"),
]
for hexb, text in VECTORS:
    i = dis.decode(bytes.fromhex(hexb), 0x140001000)
    check(f"{hexb:<20} -> {text}", i is not None and i.text == text and i.length == len(hexb) // 2)
i = dis.decode(bytes.fromhex("e859ffffff"), 0x1400010A2)
check("call rel32 -> target", i.text.startswith("call") and "140001000" in i.text)
i = dis.decode(bytes.fromhex("7f21"), 0xFFFFF3EB705AD360)
check("jg rel8 -> target", i.text == "jg      fffff3eb705ad383")
check("decode_all stops at count", len(dis.decode_all(bytes.fromhex("9090909090"), 0, 3)) == 3)

# ----------------------------- helpers -----------------------------
print("== CONTEXT / names ==")
regs = {r: 0x1111111111111111 * (n + 1) & 0xFFFFFFFFFFFFFFFF for n, r in enumerate(
    ["rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi", "r8", "r9", "r10", "r11",
     "r12", "r13", "r14", "r15", "rip"])}
back = wineye.parse_context(wineye.build_context(regs))
check("CONTEXT round trip (17 registers)", all(back[r] == v for r, v in regs.items()))
check("CONTEXT size", len(wineye.build_context(regs)) == wineye.CONTEXT_SIZE)
check("undecorate ?Func@Class@Ns@@", wineye.undecorate("?_MessageLoop@CDesktopBrowser@@IEAAXXZ")
      == "CDesktopBrowser::_MessageLoop")
check("undecorate constructor", wineye.undecorate("??0CThread@ComTaskPool@@QEAA@XZ")
      == "ComTaskPool::CThread::CThread")
check("undecorate destructor", wineye.undecorate("??1CThread@@QEAA@XZ") == "CThread::~CThread")
check("undecorate leaves C names", wineye.undecorate("KeBugCheckEx") == "KeBugCheckEx")
check("bugcheck 0xD1 name", "DRIVER_IRQL_NOT_LESS_OR_EQUAL" in wineye.bugcheck_name(0xD1))
check("bugcheck 0x3B name", "SYSTEM_SERVICE_EXCEPTION" in wineye.bugcheck_name(0x3B))

# ----------------------------- private PDB -----------------------------
print("== PDB (private) ==")
pdb = wineye.Pdb(DATA / "fixture.pdb")
check("private PDB detected", pdb.has_private_info)
rva = {n: pdb.address_of(n) for n in ("inner", "outer", "entry")}
check("publics inner/outer/entry", rva == {"inner": 0x1000, "outer": 0x1070, "entry": 0x10C0})
check("symbol_at inner+0x31", pdb.symbol_at(0x1031) == ("inner", 0x31))
check("procedure sizes", [pdb.procedure_at(rva[n])[1] for n in ("inner", "outer", "entry")] == [92, 74, 19])
lines = [pdb.line_at(r) for r in (0x1000, 0x100D, 0x1031, 0x10A2, 0x10C4)]
check("source file", all(ln and ln[0].lower().endswith("fixture.c") for ln in lines))
check("source lines", [ln[1] for ln in lines] == [9, 10, 14, 21, 27])
proc, loc = pdb.locals_at(0x1031)
byname = {v["name"]: v for v in loc}
check("locals_at -> inner", proc == "inner")
check("params depth, pt", byname["depth"]["param"] and byname["pt"]["param"])
check("locals local_counter, buffer", not byname["local_counter"]["param"] and not byname["buffer"]["param"])
check("frame offsets (rsp-relative)", byname["depth"]["where"] == ("regrel", "rsp", 0x40)
      and byname["pt"]["where"] == ("regrel", "rsp", 0x48)
      and byname["local_counter"]["where"] == ("regrel", "rsp", 0)
      and byname["buffer"]["where"] == ("regrel", "rsp", 8))
check("type kinds", pdb.type_kind(byname["pt"]["type"]) == "pointer"
      and pdb.type_kind(byname["buffer"]["type"]) == "array")
ti = pdb.find_type("_POINT3")
check("struct _POINT3 size 16", pdb.type_size(ti) == 16)
check("struct fields x,y,z", [f[:2] for f in pdb.fields(ti)] == [("x", 0), ("y", 4), ("z", 8)])
check("field_offset z", pdb.field_offset("_POINT3", "z")[0] == 8)

# ----------------------------- stack unwinding -----------------------------
print("== stack (synthetic process) ==")
BASE = 0x140000000
PID = 1234
image = (DATA / "fixture.exe").read_bytes()
pe = wineye.PeFile(image)
mem = {}                                # page -> bytearray


def poke(addr, data):
    for k, b in enumerate(data):
        a = addr + k
        mem.setdefault(a & ~0xFFF, bytearray(0x1000))[a & 0xFFF] = b


poke(BASE, image[:pe.header_size])
for va, vsize, rawptr, rawsize in pe.sections:
    poke(BASE + va, image[rawptr:rawptr + rawsize])

# State at inner+0x31: entry -> outer(7) -> inner(7, &p), local_counter = 21, buffer "w"
S = 0x14F000                            # rsp at entry
poke(S, struct.pack("<Q", 0))           # entry's return address: end of stack
poke(S - 0x30, struct.pack("<Q", BASE + 0x10CE))          # return into entry
poke(S - 0x28, struct.pack("<I", 7))                      # outer: seed (home slot)
poke(S - 0x50, struct.pack("<iiq", 7, 8, 0x1122334455667788))   # outer: p
poke(S - 0x80, struct.pack("<Q", BASE + 0x10A7))          # return into outer
poke(S - 0x78, struct.pack("<I", 7))                      # inner: depth
poke(S - 0x70, struct.pack("<Q", S - 0x50))               # inner: pt
poke(S - 0xB8, struct.pack("<i", 21))                     # inner: local_counter
poke(S - 0xB0, b"w\0")                                    # inner: buffer


class FakeEngine:
    def pids(self):
        return [4, PID]

    def process(self, pid):
        return {"pid": pid, "ppid": 4, "name": "fixture.exe" if pid == PID else "System",
                "eprocess": 0, "peb": 0, "dtb": 0, "wow64": False}

    def modules(self, pid):
        if pid != PID:
            return []
        return [{"base": BASE, "size": 0x4000, "name": "fixture.exe", "path": r"C:\fixture.exe"}]

    def read(self, pid, addr, size):
        out = bytearray()
        while size > 0:
            page = mem.get(addr & ~0xFFF)
            if page is None:
                break
            take = min(size, 0x1000 - (addr & 0xFFF))
            out += page[addr & 0xFFF:(addr & 0xFFF) + take]
            addr += take
            size -= take
        if not out:
            raise wineye.VmmError(f"lecture impossible @ {addr:#x}")
        return bytes(out)


store = wineye.SymbolStore(sympath=[str(DATA)], download=False)
kd = wineye.KernelDebugger(FakeEngine(), store, dis, log=lambda m: None)
kd.pid = PID
regs = {r: 0 for r in ("rax", "rcx", "rdx", "rbx", "rbp", "rsi", "rdi", "r8", "r9", "r10",
                       "r11", "r12", "r13", "r14", "r15")}
regs.update(rip=BASE + 0x1031, rsp=S - 0xB8)
frames = kd.stack(dict(regs), set(regs), pid=PID)
check("3 frames", len(frames) == 3)
check("symbols inner/outer/entry", [f.symbol for f in frames] ==
      ["fixture!inner+0x31", "fixture!outer+0x37", "fixture!entry+0xe"])
check("frame rsp", [f.rsp for f in frames] == [S - 0xB8, S - 0x78, S - 0x28])
check("frame source lines", [f.line[1] if f.line else None for f in frames] == [14, 21, 27])

repl = wineye.Repl(FakeEngine(), kd=kd)
with contextlib.redirect_stdout(io.StringIO()):
    repl._set_context(dict(regs), set(regs), "test", None, PID)
out_k = output(repl.run, "k")
check("k shows the three functions", all(n in out_k for n in ("fixture!inner", "fixture!outer", "fixture!entry")))
check("k shows source lines", all(f"fixture.c @ {n}]" in out_k for n in (14, 21, 27)))
out_dv = output(repl.run, "dv")
check("dv: depth = 7", "depth" in out_dv and "0n7" in out_dv)
check("dv: local_counter = 21", "local_counter" in out_dv and "0n21" in out_dv)
check("dv: pt -> &p", f"{S - 0x50:#018x}" in out_dv)
check("dv: buffer 'w'", "'w'" in out_dv)
output(repl.run, ".frame 1")
out_dv1 = output(repl.run, "dv")
check(".frame 1 + dv: seed = 7", "seed" in out_dv1 and "0n7" in out_dv1)
check(".frame 1 + dv: p (struct)", "88 77 66 55 44 33 22 11" in out_dv1)
check("ln", "fixture!outer+0x37" in output(repl.run, f"ln {BASE + 0x10A7:#x}"))
out_u = output(repl.run, "u fixture!entry L3")
check("u fixture!entry", "sub     rsp,28h" in out_u and "fixture!outer" in out_u)
out_uf = output(repl.run, "uf fixture!inner")
check("uf fixture!inner ends with ret", out_uf.rstrip().splitlines()[-1].rstrip().endswith("ret"))
check("uf fixture!inner: 25 instructions", sum(1 for ln in out_uf.splitlines()
                                               if ln.startswith("00000001400010")) == 25)

# ----------------------------- crash dump -----------------------------
print("== crash dump (synthetic PAGEDU64) ==")
PAGES = 16                              # physical pages 0..15, one run
phys = bytearray(PAGES * 0x1000)
DTB, PDPT, PD, PT, DATA_PAGE = 1, 2, 3, 4, 5
VA = 0xFFFFF80000123000
VA2M = 0xFFFFF80000400000               # 2 MB page -> PFN 0 (pdbase), reads phys page 6 at +0x6000


def pte(pfn, large=False):
    return (pfn << 12) | 0x3 | (0x80 if large else 0)


def setq(pfn, idx, val):
    struct.pack_into("<Q", phys, pfn * 0x1000 + idx * 8, val)


setq(DTB, (VA >> 39) & 0x1FF, pte(PDPT))
setq(PDPT, (VA >> 30) & 0x1FF, pte(PD))
setq(PD, (VA >> 21) & 0x1FF, pte(PT))
setq(PT, (VA >> 12) & 0x1FF, pte(DATA_PAGE))
setq(PD, (VA2M >> 21) & 0x1FF, pte(0, large=True))
phys[DATA_PAGE * 0x1000 + 0x10:DATA_PAGE * 0x1000 + 0x20] = b"wineye-dump-test"
phys[6 * 0x1000:6 * 0x1000 + 8] = b"LARGEPG!"

hdr = bytearray(0x2000)
hdr[0:8] = b"PAGEDU64"
struct.pack_into("<IIQQQQIII", hdr, 0x8, 15, 19041, DTB << 12, 0, 0xFFFFF80000001000,
                 0xFFFFF80000002000, 0x8664, 2, 0xD1)
struct.pack_into("<4Q", hdr, 0x40, 0x10, 2, 0, 0xFFFFF80000123456)
struct.pack_into("<IIQ", hdr, 0x88, 1, 0, PAGES)
struct.pack_into("<QQ", hdr, 0x98, 0, PAGES)
hdr[0x348:0x348 + 0x4D0] = wineye.build_context(dict(regs, rip=0xFFFFF80000123456,
                                                    rsp=0xFFFFF80000124000))[:0x4D0]
struct.pack_into("<I", hdr, 0xF98, 1)
with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "synthetic.dmp")
    with open(path, "wb") as f:
        f.write(hdr + phys)
    d = wineye.CrashDump(path)
    try:
        check("header: type full", d.dump_type == 1 and d.processors == 2)
        check("header: bugcheck 0xD1 + params", d.bugcheck_code == 0xD1 and d.bugcheck_params[3] == 0xFFFFF80000123456)
        check("header: DTB / PsLoadedModuleList", d.dtb == DTB << 12 and d.ps_loaded_module_list == 0xFFFFF80000001000)
        check("header: CONTEXT rip", wineye.parse_context(d.context)["rip"] == 0xFFFFF80000123456)
        check("physical read", d.read_phys(DATA_PAGE * 0x1000 + 0x10, 16) == b"wineye-dump-test")
        check("4 KB translation", d.translate(d.dtb, VA + 0x10) == DATA_PAGE * 0x1000 + 0x10)
        check("virtual read", d.read_virtual(d.dtb, VA + 0x10, 16) == b"wineye-dump-test")
        check("2 MB page translation", d.read_virtual(d.dtb, VA2M + 0x6000, 8) == b"LARGEPG!")
        check("unmapped VA -> None", d.translate(d.dtb, VA + 0x1000) is None)
        check("beyond the dump -> short read", d.read_phys(PAGES * 0x1000, 8) == b"")
    finally:
        d.close()

print()
print("RESULT:", "ALL PASS" if _failures == 0 else f"{_failures} FAILURE(S)")
sys.exit(1 if _failures else 0)
