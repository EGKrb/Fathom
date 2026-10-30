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
- user-mode minidump engine on a synthetic MDMP: ~, !analyze -v (exception), k, dv;
- x86 / WoW64: FPO program strings, x86 public names, tests/data/fixture32.pdb
  (FrameData), x86 unwinding through FPO and ebp frames, a WoW64 thread (32-bit
  context in the TEB's CPU area) and one running 32-bit code, kb / dv / r / u / uf on
  x86 frames, an x86 minidump, and the 32-bit loader list of a WoW64 process (PEB32);
- x86 code without PDB: stack layout from the code (push / pop / ebp frame / ret N),
  return addresses checked against their call, function starts after int 3 padding,
  the ebp chain as an upper bound; module data from the VADs, vmm.dll's zero-filled
  pages, a minidump whose thread stacks lie inside its memory list.

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

print("== disassembler (32-bit mode) ==")
VECTORS32 = [
    ("55", "push    ebp"),
    ("8bec", "mov     ebp,esp"),
    ("83ec10", "sub     esp,10h"),
    ("40", "inc     eax"),
    ("4f", "dec     edi"),
    ("60", "pushad"),
    ("61", "popad"),
    ("9c", "pushfd"),
    ("06", "push    es"),
    ("27", "daa"),
    ("a1d0204000", "mov     eax,dword ptr [4020D0h]"),
    ("8b0d00304000", "mov     ecx,dword ptr [403000h]"),
    ("ff15e8204000", "call    dword ptr [4020E8h]"),
    ("8b4508", "mov     eax,dword ptr [ebp+8]"),
    ("64a118000000", "mov     eax,dword ptr fs:[18h]"),
    ("c20800", "ret     8"),
    ("6633c0", "xor     ax,ax"),
    ("9bdfe0", "fstsw   ax"),
    ("d9eb", "fldpi"),
]
for hexb, text in VECTORS32:
    i = dis.decode(bytes.fromhex(hexb), 0x401000, mode32=True)
    check(f"{hexb:<20} -> {text}", i.text == text and i.length == len(hexb) // 2)
i = dis.decode(bytes.fromhex("e8fb0f0000"), 0x401000, mode32=True)
check("call rel32 -> 32-bit target", i.text == "call    00402000" and i.target == 0x402000)
check("same bytes in long mode: REX, not inc", dis.decode(bytes.fromhex("4889e5"), 0).text == "mov     rbp,rsp")
check("SSE cmpsd: no rep prefix", dis.decode(bytes.fromhex("f20fc2c800"), 0).text == "cmpsd   xmm1,xmm0,0")

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


def build_minidump(path, ranges, threads, modules, exc, arch=9):
    n_streams = 6
    base = 32 + 12 * n_streams
    blob = bytearray()

    def place(b):
        off = base + len(blob)
        blob.extend(b)
        return off
    si_rva = place(struct.pack("<HHHBBIIIIIHH", arch, 6, 0, 1, 1, 10, 0, 19045, 2, 0, 0, 0) + b"\0" * 24)
    misc_rva = place(struct.pack("<IIIIII", 24, 1, PID, 0, 0, 0))
    name_rvas = [place(struct.pack("<I", len(nm.encode("utf-16-le"))) + nm.encode("utf-16-le") + b"\0\0")
                 for *_x, nm in modules]
    ml = struct.pack("<I", len(modules)) + b"".join(
        struct.pack("<QIIII", b, sz, cs, ts, nr) + b"\0" * 84 for (b, sz, cs, ts, _n), nr in zip(modules, name_rvas))
    ml_rva = place(ml)
    ctx_rvas = [place(c) for _t, _teb, c, *_s in threads]
    tl = struct.pack("<I", len(threads)) + b"".join(
        struct.pack("<IIIIQQIIII", tid, 0, 0, 0, teb, *(st[0] if st else (0, 0)), 0, len(c), cr)
        for (tid, teb, c, *st), cr in zip(threads, ctx_rvas))
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
    data_at = {}
    for start, data in ranges:
        data_at[start] = base + len(blob)
        blob.extend(data)
    for i, (_tid, _teb, _c, *st) in enumerate(threads):
        if st:                       # stack range: its RVA inside the memory list data
            start = max(a for a in data_at if a <= st[0][0])
            struct.pack_into("<I", blob, tl_rva - base + 4 + 48 * i + 32, data_at[start] + st[0][0] - start)
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

# ----------------------------- x86 / WoW64 -----------------------------
print("== x86: FPO programs, public names ==")
check("undecorate_x86 stdcall", wineye.undecorate_x86("_NtDelayExecution@8") == "NtDelayExecution")
check("undecorate_x86 one underscore only", wineye.undecorate_x86("___RtlUserThreadStart@8") == "__RtlUserThreadStart")
check("undecorate_x86 fastcall / cdecl", wineye.undecorate_x86("@RtlpFoo@12") == "RtlpFoo"
      and wineye.undecorate_x86("_memcpy") == "memcpy")
check("undecorate_x86 leaves C++ names", wineye.undecorate_x86("?f@@YAXXZ") == "?f@@YAXXZ")
words = {0x1000: 0x2000, 0x1004: 0x401234, 0x0FF8: 0x5E5E}
out = wineye._eval_fpo_program("$T0 $ebp = $eip $T0 4 + ^ = $ebp $T0 ^ = $esp $T0 8 + =  $23 $T0 8 - ^ = ",
                               {"$ebp": 0x1000, "$esp": 0xF00, "$eip": 0x401000}, words.__getitem__)
check("FPO program: ebp frame", (out["$eip"], out["$ebp"], out["$esp"], out["$esi"]) == (0x401234, 0x2000, 0x1008, 0x5E5E))
check("FPO program: assigned names", out["="] == {"$T0", "$eip", "$ebp", "$esp", "$esi"})
out = wineye._eval_fpo_program("$T1 .raSearch = $T0  $T1 4 - 8 @ = $eip $T1 ^ = $esp $T1 4 + =",
                               {".raSearch": 0x1004, "$esp": 0}, words.__getitem__)
check("FPO program: .raSearch and @ (align)", (out["$T0"], out["$eip"], out["$esp"]) == (0x1000, 0x401234, 0x1008))

print("== x86: PDB FrameData (fixture32.pdb) ==")
pdb32 = wineye.Pdb(DATA / "fixture32.pdb")
check("x86 PDB, publics undecorated", pdb32.machine == 0x14C and pdb32.address_of("inner32") == 0x1010
      and pdb32.address_of("outer32") == 0x1050 and pdb32.address_of("entry") == 0x1000)
fd = pdb32.frame_data(0x1027)
check("FrameData of inner32 after push esi", fd["start"] == 0x1015 and fd["saved"] == 4 and fd["params"] == 8
      and "$23" in fd["program"])
check("FrameData of outer32: ebp frame", pdb32.frame_data(0x1067)["use_bp"])

print("== x86: stack (FPO + ebp frames) ==")
BASE32 = 0x400000
image32 = (DATA / "fixture32.exe").read_bytes()
pe32 = wineye.PeFile(image32)
poke(BASE32, image32[:pe32.header_size])
for va, vsize, rawptr, rawsize in pe32.sections:
    poke(BASE32 + va, image32[rawptr:rawptr + rawsize])
# entry -> outer32(7) -> inner32(7, &value), in its loop: FPO, esi pushed; outer32 has an ebp frame
T = 0x18FF00                            # esp at entry
poke(T, struct.pack("<I", 0))           # entry's return address: end of stack
poke(T - 8, struct.pack("<II", BASE32 + 0x1007, 7))       # return into entry, seed
poke(T - 0x10, struct.pack("<II", 0, 0))                  # outer32: value, caller's ebp
poke(T - 0x20, struct.pack("<IIII", 0xABCDEF, BASE32 + 0x1067, 7, T - 0x10))   # esi, ret, depth, out
R32 = {"rip": BASE32 + 0x1027, "rsp": T - 0x20, "rbp": T - 0xC, "rax": 0x52, "rcx": 0, "rdx": 0x15,
       "rbx": 0, "rsi": 0xC, "rdi": 0, "cs": 0x23}


class Wow64Engine(FakeEngine):
    """A WoW64 process: fixture.exe (x64) stands for wow64cpu.dll, fixture32.exe is the
    32-bit program."""

    def modules(self, pid):
        return [{"base": BASE, "size": 0x4000, "name": "wow64cpu.dll", "path": r"C:\Windows\System32\wow64cpu.dll"},
                {"base": BASE32, "size": pe32.size_of_image, "name": "fixture32.exe", "path": r"C:\fixture32.exe",
                 "wow64": True}]


kd32 = wineye.KernelDebugger(Wow64Engine(), store, dis, log=lambda m: None)
kd32.pid = PID
fr = kd32.stack(dict(R32), set(R32), pid=PID)
check("32-bit context (cs 0x23): 3 x86 frames", [f.symbol for f in fr] ==
      ["fixture32!inner32+0x17", "fixture32!outer32+0x17", "fixture32!entry+0x7"] and all(f.x86 for f in fr))
check("x86 frame esp", [f.rsp for f in fr] == [T - 0x20, T - 0x18, T - 4])
check("esi restored from inner32's FrameData", fr[1].regs["rsi"] == 0xABCDEF and "rsi" in fr[1].known)
check("x86 source lines", [f.line[1] if f.line else None for f in fr] == [14, 26, 33])


class NoPdbStore(wineye.SymbolStore):
    def for_image(self, read, base):
        return None


print("== x86 without PDB: the stack layout from the code ==")
nopdb32 = wineye.KernelDebugger(Wow64Engine(), NoPdbStore(download=False), dis, log=lambda m: None)
fr = nopdb32.stack(dict(R32, rip=BASE32 + 0x103F, rsp=T - 0x1C), set(R32), pid=PID)
check("at the ret of inner32", [f.rip for f in fr] == [BASE32 + 0x103F, BASE32 + 0x1067, BASE32 + 0x1007])
check("layout of inner32 in its loop: esi pushed", nopdb32._x86_layout(BASE32 + 0x1010, BASE32 + 0x1027, PID)
      == (4, None, {"rsi": 4}))
check("layout of outer32 at its call: ebp frame, 2 arguments pushed",
      nopdb32._x86_layout(BASE32 + 0x1050, BASE32 + 0x1062, PID) == (16, 4, {"rbp": 4}))
check("call before a return address", nopdb32._call_before(BASE32 + 0x1067, PID) == (BASE32 + 0x1062, BASE32 + 0x1010))
check("not after a call / code not in memory", nopdb32._call_before(BASE32 + 0x1050, PID) == ()
      and nopdb32._call_before(0x7000000, PID) is None)
check("function start after int 3 padding", BASE32 + 0x1050 in nopdb32._x86_starts_before(BASE32 + 0x1062,
                                                                                         nopdb32.module_at(BASE32, PID), PID))
fr = nopdb32.stack(dict(R32), set(R32), pid=PID)
check("FPO loop, no PDB: the 3 frames", [f.rip for f in fr] == [BASE32 + 0x1027, BASE32 + 0x1067, BASE32 + 0x1007]
      and [f.rsp for f in fr] == [T - 0x20, T - 0x18, T - 4] and not any(f.note == "scan" for f in fr))
check("FPO loop, no PDB: esi and ebp restored from the pushes", fr[1].regs["rsi"] == 0xABCDEF and fr[2].regs["rbp"] == 0)
stack32 = nopdb32.read(T - 0x18, 0x40, PID)
outer_regs = dict(fr[1].regs)
check("a frame above the ebp chain is refused", nopdb32._x86_frame_at(BASE32 + 0x1050, BASE32 + 0x1062, outer_regs,
                                                                      stack32, PID) == (T - 8, {"rbp": 4})
      and nopdb32._x86_frame_at(BASE32 + 0x1050, BASE32 + 0x1062, outer_regs, stack32, PID, limit=T - 0xC) is None)

print("== WoW64 thread (32-bit context in the TEB) ==")
TEB, CPU = 0x7F0000, 0x7F4000
poke(TEB + 0x1488, struct.pack("<Q", CPU))               # TlsSlots[WOW64_TLS_CPURESERVED]
ctx32 = bytearray(wineye.CONTEXT32_SIZE)
struct.pack_into("<I", ctx32, 0, 0x1003F)
for r, o in wineye._CTX32_OFF.items():
    struct.pack_into("<I", ctx32, o, R32[r])
struct.pack_into("<II", ctx32, 0xBC, 0x23, 0x246)
poke(CPU, struct.pack("<HH", 2, 0x14C) + bytes(ctx32))    # Flags, Machine, WOW64_CONTEXT
r32 = wineye.parse_context32(bytes(ctx32))
check("x86 CONTEXT: registers under their 64-bit names", wineye.is_context32(bytes(ctx32))
      and all(r32[r] == v for r, v in R32.items()))
check("x64 CONTEXT is not x86", not wineye.is_context32(wineye.build_context(dict(regs))))
fr = kd32.stack(dict(regs, cs=0x33), set(regs), pid=PID, teb=TEB)
check("x64 layer + x86 frames between the wow64cpu entry and its loop", [f.symbol for f in fr] ==
      ["wow64cpu!inner+0x31", "wow64cpu!outer+0x37", "fixture32!inner32+0x17", "fixture32!outer32+0x17",
       "fixture32!entry+0x7", "wow64cpu!entry+0xe"])
check("frames renumbered, x86 flags", [f.index for f in fr] == list(range(6))
      and [f.x86 for f in fr] == [False, False, True, True, True, False])
fr = kd32.stack(dict(regs, cs=0x33), set(regs), pid=PID)
check("no TEB: x64 frames only, the missing 32-bit context said", len(fr) == 3 and fr[1].note == "nowow64"
      and "absents de la source" in wineye.frame_transition(fr, 1))
repl32 = wineye.Repl(Wow64Engine(), kd=kd32)
with contextlib.redirect_stdout(io.StringIO()):
    repl32._set_context(dict(regs, cs=0x33), set(regs), "test", None, PID, teb=TEB)
out_k = output(repl32.run, "kb")
check("k: 32-bit code separators", "--- code 32 bits (WoW64) ---" in out_k and "--- couche WoW64 (code 64 bits) ---" in out_k)
check("kb: x86 stack parameters of inner32", f"00000007 {T - 0x10:08x} 00000000" in out_k)
output(repl32.run, ".frame 2")
out_dv = output(repl32.run, "dv")
check("dv on an x86 FPO frame: depth, out", "depth" in out_dv and "0n7" in out_dv and f"{T - 0x10:#010x}" in out_dv)
out_r = output(repl32.run, "r")
check("r on an x86 frame", f"eip={BASE32 + 0x1027:08x}" in out_r and f"esp={T - 0x20:08x}" in out_r and "r8=" not in out_r)
check("eip in expressions", repl32.eval_expr("eip+1") == BASE32 + 0x1028)
output(repl32.run, ".frame 3")
check("dv on an ebp frame: seed, value", all(x in output(repl32.run, "dv") for x in ("seed", "0n7", "value", "0n0")))
check("u decodes 32-bit code", "push    ebp" in output(repl32.run, "u fixture32!outer32 L1"))
out_uf = output(repl32.run, "uf fixture32!inner32")
check("uf fixture32!inner32 (no .pdata): 17 instructions, ret 8", out_uf.rstrip().endswith("ret     8")
      and sum(1 for ln in out_uf.splitlines() if ln.startswith("0000000000401")) == 17)

print("== x86 minidump ==")
ctx_crash32 = bytes(ctx32)
with tempfile.TemporaryDirectory() as tmp:
    md = os.path.join(tmp, "fixture32.dmp")
    build_minidump(md, sorted((a, bytes(b)) for a, b in mem.items() if a < 1 << 32),
                   [(0x333, 0x7FD000, ctx_crash32)],
                   [(BASE32, pe32.size_of_image, pe32.checksum, pe32.timestamp, r"C:\fixture32.exe")],
                   (0x333, 0xC0000005, BASE32 + 0x1027, [0, 0x10], ctx_crash32), arch=0)
    eng = wineye.MinidumpEngine(md, error_cls=wineye.VmmError)
    try:
        check("engine: x86 minidump", eng.x86 and eng.process(0)["system"].startswith("windows-x86"))
        kdm = wineye.KernelDebugger(eng, store, dis, log=lambda m: None)
        rm = wineye.Repl(eng, kd=kdm)
        rm.pid = kdm.pid = eng.pid
        check("~ on x86 contexts", "fixture32!inner32+0x17" in output(rm.run, "~"))
        out_a = output(rm.run, "!analyze -v")
        check("!analyze: 32-bit faulting instruction", "add     edx,esi" in out_a)
        check("!analyze: x86 stack", all(x in out_a for x in ("fixture32!inner32+0x17", "fixture32!outer32+0x17",
                                                                "fixture32!entry+0x7")))
    finally:
        eng.close()
    # a full-memory minidump whose thread stack range lies inside its memory list
    # (wineye's .dump /u): what is above the stack range (WoW64 CPU area) stays readable
    md2 = os.path.join(tmp, "overlap.dmp")
    page = T & ~0xFFF
    build_minidump(md2, [(page, bytes(mem[page]))],
                   [(0x444, 0x7FD000, ctx_crash32, (T - 0x20, 0x10))], [], (0x444, 0, 0, [], ctx_crash32), arch=0)
    eng = wineye.MinidumpEngine(md2, error_cls=wineye.VmmError)
    try:
        check("minidump: memory past a thread's stack range", eng.read(PID, T - 0x20, 0x24)[-4:] == bytes(4)
              and eng.read(PID, T - 8, 4) == struct.pack("<I", BASE32 + 0x1007))
    finally:
        eng.close()

print("== module data from the VADs, zero-filled pages (vmm.dll) ==")


class PagedOutEngine(Wow64Engine):
    """vmm.dll style: pages it cannot read come back as zeros; loader strings paged out."""

    def modules(self, pid):
        return [{"base": BASE32, "size": pe32.size_of_image, "name": "", "path": "", "wow64": True},
                {"base": BASE, "size": 0x4000, "name": "_64-ntdll.dll", "path": ""}]

    def read(self, pid, addr, size):
        if BASE32 <= addr < BASE32 + pe32.size_of_image:
            return bytes(size)
        return super().read(pid, addr, size)


class VadKd32(wineye.KernelDebugger):
    def _vad_modules(self, pid):
        m = wineye.Module("fixture32.exe", BASE32, pe32.size_of_image, r"\fixture32.exe", pe32.timestamp,
                          pe32.checksum, len(image32), from_vad=True)
        return [m]


kv32 = VadKd32(PagedOutEngine(), wineye.SymbolStore(sympath=[str(DATA)], download=False), dis, log=lambda m: None)
kv32.pid = PID
mods = kv32.user_modules(PID)
check("name paged out: taken from the VADs", [m.name for m in mods] == ["fixture32.exe", "ntdll.dll"]
      and mods[0].timestamp == pe32.timestamp)
check("zero-filled header: image found locally, symbols", kv32.symbolize(BASE32 + 0x1027, PID)[0] == "fixture32!inner32+0x17")

print("== WoW64 modules in a kernel dump (PEB32 loader list) ==")
vmem = {}


def vpoke(addr, data):
    for k, b in enumerate(data):
        vmem[addr + k] = b


class FakeDump:
    def read_virtual(self, dtb, va, size):
        return bytes(vmem.get(va + i, 0) for i in range(size))


PEB32, LDR32, E1, E2, KWOW = 0x300000, 0x301000, 0x302000, 0x303000, 0xFFFF800000500000
vpoke(KWOW, struct.pack("<Q", PEB32))                      # _EWOW64PROCESS.Peb
vpoke(PEB32 + 0x0C, struct.pack("<I", LDR32))              # PEB32.Ldr
vpoke(LDR32 + 0x0C, struct.pack("<II", E1, E2))            # InLoadOrderModuleList
for ent, nxt, base, name in ((E1, E2, BASE32, "fixture32.exe"), (E2, LDR32 + 0x0C, 0x77230000, "ntdll.dll")):
    u16 = name.encode("utf-16-le")
    vpoke(ent, struct.pack("<I", nxt))
    vpoke(ent + 0x18, struct.pack("<III", base, base + 0x1000, 0x4000))
    vpoke(ent + 0x24, struct.pack("<HHI", len(u16), len(u16), ent + 0x100))
    vpoke(ent + 0x2C, struct.pack("<HHI", len(u16), len(u16), ent + 0x100))
    vpoke(ent + 0x44, struct.pack("<I", 0x12345678))
    vpoke(ent + 0x100, u16)
ne = wineye.NativeEngine.__new__(wineye.NativeEngine)
ne.dump, ne.kdtb, ne._umods = FakeDump(), 0, {}
ne._procs = {PID: {"pid": PID, "peb": 0, "peb32": ne._peb32(KWOW), "dtb": 0}}
check("PEB32 from EWOW64PROCESS / directly (Windows 7)", ne._peb32(KWOW) == PEB32 and ne._peb32(PEB32) == PEB32
      and ne._peb32(0) == 0)
mods32 = ne.modules(PID)
check("32-bit modules listed", [(m["name"], m["base"], m["wow64"]) for m in mods32] ==
      [("fixture32.exe", BASE32, True), ("ntdll.dll", 0x77230000, True)] and mods32[0]["timestamp"] == 0x12345678)

print()
print("RESULT:", "ALL PASS" if _failures == 0 else f"{_failures} FAILURE(S)")
sys.exit(1 if _failures else 0)
