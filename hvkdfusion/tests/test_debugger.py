"""Tests of wineye's debugger layer, without any real target.

- x86-64 disassembler (WinDbg syntax) on known vectors;
- CONTEXT record round trip, MSVC name undecoration, bugcheck names;
- private PDB reader on tests/data/fixture.pdb (built from fixture.c): publics,
  procedures, source lines, parameters / locals, struct layouts;
- x64 stack unwinding (.pdata / UNWIND_INFO) on a synthetic process that maps
  tests/data/fixture.exe and a hand-built stack entry -> outer -> inner, then k, dv,
  .frame, ln, u, uf through the REPL;
- native crash-dump reader on a synthetic PAGEDU64 full dump (header, runs, 4-level
  page tables with 4 KB / 2 MB pages);
- kb / dv without a PDB (parameter home slots);
- a local image recognized by SizeOfImage + CheckSum + file size;
- user modules rebuilt from the VADs when a kernel dump has no user pages (with and
  without the image TimeDateStamp in the kernel), symbolization through the image;
- user-mode minidump engine on a synthetic MDMP: ~, !analyze -v (exception), k, dv.

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
check(".frame 1 + dv: p (struct fields)", "{x=7 y=8 z=0x1122334455667788}" in out_dv1)
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

# ----------------------------- kb / dv without a private PDB -----------------------------
print("== kb, dv without PDB ==")
kd_nopdb = wineye.KernelDebugger(FakeEngine(), wineye.SymbolStore(download=False), dis, log=lambda m: None)
kd_nopdb.pid = PID
repl_np = wineye.Repl(FakeEngine(), kd=kd_nopdb)
with contextlib.redirect_stdout(io.StringIO()):
    repl_np._set_context(dict(regs), set(regs), "test", None, PID)
out_kb = output(repl_np.run, "kb")
check("kb: Args to Child of inner = depth, &p", f"0000000000000007 {S - 0x50:016x}" in out_kb)
out_dvp = output(repl_np.run, "dv")
check("dv without PDB: says so", "pas de PDB pour fixture" in out_dvp)
check("dv without PDB: home slots", "arg1  0000000000000007" in out_dvp and f"arg2  {S - 0x50:016x}" in out_dvp)

# ----------------------------- local image identification -----------------------------
print("== local image (SizeOfImage + CheckSum + file size) ==")
store_local = wineye.SymbolStore(sympath=[str(DATA)], download=False)
img = store_local.image("fixture.exe", 0, pe.size_of_image, pe.checksum, len(image))
check("matching copy found", img is not None and img.timestamp == pe.timestamp)
check("wrong file size -> rejected", store_local.image("fixture.exe", 0, pe.size_of_image, pe.checksum, len(image) + 1) is None)
check("wrong SizeOfImage -> rejected", store_local.image("fixture.exe", 0, pe.size_of_image + 0x1000, pe.checksum, len(image)) is None)
check("SizeOfImage alone -> not enough", store_local.image("fixture.exe", 0, pe.size_of_image, 0, 0) is None)

# ----------------------------- modules from the VADs (kernel dump) -----------------------------
print("== modules from the VADs (no user pages) ==")
kmem = {}


def kpoke(addr, data):
    for k, b in enumerate(data):
        a = addr + k
        kmem.setdefault(a & ~0xFFF, bytearray(0x1000))[a & 0xFFF] = b


VAD, SUB, CA, FO, NAME, SEG, INFO = (0xFFFF800000010000 + 0x10000 * i for i in range(7))
path_u16 = "\\Windows\\System32\\fixture.exe".encode("utf-16-le")
kpoke(VAD + 0x48, struct.pack("<Q", SUB))                  # _MMVAD.Subsection
kpoke(SUB, struct.pack("<Q", CA))                          # _SUBSECTION.ControlArea
kpoke(CA, struct.pack("<Q", SEG))                          # _CONTROL_AREA.Segment
kpoke(CA + 0x38, struct.pack("<I", 1 << 5))                # u.Flags.Image
kpoke(CA + 0x40, struct.pack("<Q", FO | 0x7))              # FilePointer (EX_FAST_REF)
kpoke(FO + 0x58, struct.pack("<HHxxxxQ", len(path_u16), len(path_u16), NAME))
kpoke(NAME, path_u16)
kpoke(SEG + 0x38, struct.pack("<Q", INFO))                 # _SEGMENT.u2.ImageInformation
kpoke(INFO + 0x38, struct.pack("<II", len(image), pe.checksum))       # ImageFileSize, CheckSum
kpoke(INFO + 0x44, struct.pack("<II", pe.size_of_image, pe.timestamp))  # SizeOfImage, TimeDateStamp


class KernelOnlyEngine(FakeEngine):
    """A kernel dump: kernel structures readable, no user-mode page, no loader list."""

    def modules(self, pid):
        return []

    def vads(self, pid):
        return [{"start": BASE, "end": BASE + 0x3FFF, "vad": VAD}]

    def read(self, pid, addr, size):
        page = kmem.get(addr & ~0xFFF)
        if page is None:
            raise wineye.VmmError("absent")
        return bytes(page[addr & 0xFFF:(addr & 0xFFF) + size])


OFFS = {("_MMVAD", "Subsection"): 0x48, ("_CONTROL_AREA", "Segment"): 0, ("_CONTROL_AREA", "FilePointer"): 0x40,
        ("_CONTROL_AREA", "u"): 0x38, ("_FILE_OBJECT", "FileName"): 0x58, ("_SEGMENT", "u2"): 0x38,
        ("_MI_SECTION_IMAGE_INFORMATION", "InternalImageInformation"): 0x40,
        ("_MI_EXTRA_IMAGE_INFORMATION", "SizeOfImage"): 4, ("_MI_EXTRA_IMAGE_INFORMATION", "TimeDateStamp"): 8,
        ("_SECTION_IMAGE_INFORMATION", "CheckSum"): 0x3C, ("_SECTION_IMAGE_INFORMATION", "ImageFileSize"): 0x38}


def vad_kd(layout):
    class VadKd(wineye.KernelDebugger):
        def off(self, typ, field, default=None):
            v = layout.get((typ, field))
            if v is None:
                if default is not None:
                    return default
                raise wineye.DbgError(f"{typ}.{field}")
            return v
    k = VadKd(KernelOnlyEngine(), wineye.SymbolStore(sympath=[str(DATA)], download=False), dis, log=lambda m: None)
    k.pid = PID
    return k


for label, layout in (("Windows 10 2004+ (TimeDateStamp in the kernel)", OFFS),
                      ("Windows 10 1803 (no TimeDateStamp: local copy)",
                       {k: v for k, v in OFFS.items() if k[1] != "TimeDateStamp"})):
    kv = vad_kd(layout)
    mods = kv.user_modules(PID)
    m = mods[0] if mods else None
    check(f"{label}: module rebuilt", m is not None and m.name == "fixture.exe" and m.base == BASE
          and m.size == pe.size_of_image and m.from_vad and m.file_size == len(image))
    check(f"{label}: timestamp", m is not None and m.timestamp == (pe.timestamp if "TimeDateStamp" in str(layout) else 0))
    check(f"{label}: symbolized through the image + PDB",
          kv.symbolize(BASE + 0x1031, PID)[0] == "fixture!inner+0x31")
    fr = kv.stack(dict(regs), set(regs), pid=PID)
    check(f"{label}: one frame, stack noted as missing", len(fr) == 1 and fr[0].note == "nostack")

# ----------------------------- user-mode minidump -----------------------------
print("== user-mode minidump (MDMP) ==")


def build_minidump(path, ranges, threads, modules, exc):
    n_streams = 6
    base = 32 + 12 * n_streams
    blob = bytearray()

    def place(b):
        off = base + len(blob)
        blob.extend(b)
        return off
    si_rva = place(struct.pack("<HHHBBIIIIIHH", 9, 6, 0, 1, 1, 10, 0, 19045, 2, 0, 0, 0) + b"\0" * 24)
    misc_rva = place(struct.pack("<IIIIII", 24, 1, PID, 0, 0, 0))
    name_rvas = [place(struct.pack("<I", len(nm.encode("utf-16-le"))) + nm.encode("utf-16-le") + b"\0\0")
                 for *_x, nm in modules]
    ml = struct.pack("<I", len(modules)) + b"".join(
        struct.pack("<QIIII", b, sz, cs, ts, nr) + b"\0" * 84 for (b, sz, cs, ts, _n), nr in zip(modules, name_rvas))
    ml_rva = place(ml)
    ctx_rvas = [place(c) for _t, _teb, c in threads]
    tl = struct.pack("<I", len(threads)) + b"".join(
        struct.pack("<IIIIQQIIII", tid, 0, 0, 0, teb, 0, 0, 0, len(c), cr)
        for (tid, teb, c), cr in zip(threads, ctx_rvas))
    tl_rva = place(tl)
    tid, code, address, params, ctx = exc
    exc_ctx_rva = place(ctx)
    ex = struct.pack("<IIIIQQII", tid, 0, code, 0, 0, address, len(params), 0)
    ex += struct.pack("<15Q", *(params + [0] * (15 - len(params)))) + struct.pack("<II", len(ctx), exc_ctx_rva)
    ex_rva = place(ex)
    m64 = bytearray(struct.pack("<QQ", len(ranges), 0))
    for start, data in ranges:
        m64 += struct.pack("<QQ", start, len(data))
    m64_rva = place(bytes(m64))
    struct.pack_into("<Q", blob, m64_rva - base + 8, base + len(blob))
    for _start, data in ranges:
        blob.extend(data)
    directory = [(7, 56, si_rva), (15, 24, misc_rva), (4, len(ml), ml_rva), (3, len(tl), tl_rva),
                 (6, len(ex), ex_rva), (9, len(m64), m64_rva)]
    with open(path, "wb") as f:
        f.write(struct.pack("<IIIIIIQ", 0x504D444D, 0xA793, n_streams, 32, 0, 0, 0))
        f.write(b"".join(struct.pack("<III", *d) for d in directory))
        f.write(blob)


ctx_crash = wineye.build_context(dict(regs))
ctx_t1 = wineye.build_context(dict(regs, rip=BASE + 0x10C9, rsp=S - 0x28))   # entry, at the call
with tempfile.TemporaryDirectory() as tmp:
    md = os.path.join(tmp, "fixture.dmp")
    build_minidump(md, sorted((a, bytes(b)) for a, b in mem.items()),
                   [(0x111, 0x7FF000, ctx_crash), (0x222, 0x7FE000, ctx_t1)],
                   [(BASE, pe.size_of_image, pe.checksum, pe.timestamp, r"C:\fixture.exe")],
                   (0x111, 0xC0000005, BASE + 0x1031, [1, 0], ctx_crash))
    eng = wineye.MinidumpEngine(md, error_cls=wineye.VmmError)
    try:
        check("engine: pid, threads, modules", eng.pid == PID and len(eng.threads) == 2 and eng.modules(0)[0]["base"] == BASE)
        check("engine: exception stream", eng.exception and eng.exception["code"] == 0xC0000005 and eng.exception["tid"] == 0x111)
        check("engine: memory read", eng.read(PID, S - 0x50, 16) == struct.pack("<iiq", 7, 8, 0x1122334455667788))
        kdm = wineye.KernelDebugger(eng, store, dis, log=lambda m: None)
        rm = wineye.Repl(eng, kd=kdm)
        rm.pid = kdm.pid = eng.pid
        out_t = output(rm.run, "~")
        check("~ lists the 2 threads", "111" in out_t and "222" in out_t and "fixture!inner+0x31" in out_t)
        out_a = output(rm.run, "!analyze -v")
        check("!analyze: exception, write at 0", "c0000005" in out_a and "criture" in out_a and "0x0000000000000000" in out_a)
        check("!analyze: faulting function and bucket", "FAULTING_IP : fixture!inner+0x31" in out_a
              and "FAILURE_BUCKET_ID : C0000005_fixture!inner" in out_a)
        out_km = output(rm.run, "k")
        check("k on the exception thread", all(x in out_km for x in ("fixture!inner+0x31", "fixture!outer+0x37", "fixture!entry+0xe")))
        out_dm = output(rm.run, "dv")
        check("dv on the minidump", "0n7" in out_dm and "0n21" in out_dm and "'w'" in out_dm)
        output(rm.run, "~1")
        out_k1 = output(rm.run, "k")
        check("~1 then k: other thread", "fixture!entry+0x9" in out_k1 and "fixture!inner" not in out_k1)
        check(".dump /u refused on a minidump", "déjà un minidump" in output(rm.run, ".dump /u x.dmp"))
    finally:
        eng.close()

print()
print("RESULT:", "ALL PASS" if _failures == 0 else f"{_failures} FAILURE(S)")
sys.exit(1 if _failures else 0)
