#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HvKdFusion - unified Hyper-V live-kernel-debug orchestrator.

Fuses the two projects into one workflow:

  * LiveCloudKd  -> the Hyper-V guest memory engine (hvlib.dll / hvmm.sys,
                    driven through the shipped hvlib.py ctypes wrapper):
                    enumerate VMs, read/write guest physical & virtual
                    memory, dump, pause/resume.
  * windbg-master -> the debugger binary (windbg.exe / kd.exe), extracted
                    from doc/windbg64.part*.rar, that we attach to a VM.

The tool has a single config.json tying both together and a single CLI:

    setup     resolve + stage both toolchains, write config.json
    config    print the resolved configuration
    list      enumerate running Hyper-V VMs (DLL engine)
    debug     attach WinDbg/kd to a VM (via LiveCloudKd.exe launcher)
    dump      dump guest memory to a file (engine, or LiveCloudKd.exe)
    read      read guest physical/virtual memory (engine)
    write     write guest physical/virtual memory (engine)
    resume    resume a paused VM

Memory operations need: admin rights, Hyper-V running, a target VM, and the
hvmm.sys driver (loaded by hvlib.dll on first use). The setup/extract/stage
steps work without any of that.

GPL3 (inherits LiveCloudKd's license for the engine it drives).
"""

import argparse
import ctypes
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"

# Default locations of the source trees (unzipped GitHub archives in the user's
# Downloads folder). setup can override every one of them.
# master2 is the fuller LiveCloudKd tree whose LiveCloudKd.exe actually builds.
DOWNLOADS = Path.home() / "Downloads"
DEFAULT_LIVECLOUDKD = DOWNLOADS / "LiveCloudKd-master2" / "LiveCloudKd-master"
DEFAULT_WINDBG = DOWNLOADS / "windbg-master" / "windbg-master"
DEFAULT_NTOSEYE = DOWNLOADS / "ntoseye-master" / "ntoseye-master"
DEFAULT_MEMPROCFS = DOWNLOADS / "MemProcFS-master" / "MemProcFS-master"
DEFAULT_DOKANY = DOWNLOADS / "dokany-master" / "dokany-master"
DEFAULT_LEECHCORE = DOWNLOADS / "LeechCore-master" / "LeechCore-master"

# LiveCloudKd.exe action codes (see its README /a option).
ACTION_LIVE_KD = 0
ACTION_EXDI_WINDBG = 1
ACTION_DUMP_LINEAR = 2
ACTION_DUMP_CRASH = 3
ACTION_DUMP_CHUNK = 4
ACTION_DUMP_RAW = 5
ACTION_RESUME = 6


# --------------------------------------------------------------------------- #
#  Config
# --------------------------------------------------------------------------- #
def default_config() -> dict:
    return {
        "livecloudkd_dir": str(DEFAULT_LIVECLOUDKD),
        "windbg_src_dir": str(DEFAULT_WINDBG),
        # resolved by `setup`:
        "hvlib_dir": "",        # folder containing hvlib.py + hvlib.dll
        "hvlib_py": "",         # path to hvlib.py
        "hvlib_dll": "",        # path to hvlib.dll
        "windbg_dir": "",       # folder containing windbg.exe / kd.exe
        "windbg_exe": "",
        "kd_exe": "",
        "livecloudkd_exe": "",  # may stay empty (source tree ships no prebuilt exe)
        "symbol_path": r"SRV*C:\Symbols*https://msdl.microsoft.com/download/symbols",
        # engine defaults (0 = WinHv hypercall, 1 = raw hvmm driver)
        "read_method": 1,
        "write_method": 1,
        # ntoseye (WinDbg-like debugger for Linux/macOS; runs under WSL here).
        # Used for offline analysis of dumps HvKdFusion produces.
        "ntoseye_src_dir": str(DEFAULT_NTOSEYE),
        "ntoseye_use_wsl": False,
        "ntoseye_path": "",   # windows path, or Linux abs path when via WSL
        # MemProcFS: mounts memory (a dump, or a live Hyper-V VM via the
        # leechcore 'hvmm' device) as a filesystem. Needs the Dokany driver.
        "memprocfs_src_dir": str(DEFAULT_MEMPROCFS),
        "memprocfs_exe": "",
        "memprocfs_dir": "",          # folder of MemProcFS.exe (plugin staging)
        "leechcore_hvmm_dll": "",     # built/prebuilt leechcore_device_hvmm.dll
        "dokany_src_dir": str(DEFAULT_DOKANY),
        "dokan_installed": False,
        # LeechCore: the memory-acquisition layer MemProcFS uses (leechcore.dll,
        # many -device backends). MemProcFS bundles leechcore.dll in its folder.
        "leechcore_src_dir": str(DEFAULT_LEECHCORE),
        "leechcore_dll": "",
    }


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        die(f"No config at {CONFIG_PATH}. Run:  python hvkdfusion.py setup")
    cfg = default_config()
    cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"[+] wrote {CONFIG_PATH}")


# --------------------------------------------------------------------------- #
#  Small helpers
# --------------------------------------------------------------------------- #
def die(msg: str, code: int = 1):
    print(f"[!] {msg}", file=sys.stderr)
    sys.exit(code)


def find_extractor() -> list[str] | None:
    """Return an argv prefix that extracts a .rar given (archive, outdir)."""
    exe = shutil.which("7z") or shutil.which("7za")
    if exe:
        return [exe, "x", "-y"]  # 7z x -y <archive> -o<dir>
    winrar = Path(r"C:\Program Files\WinRAR\WinRAR.exe")
    if winrar.exists():
        return [str(winrar)]
    unrar = shutil.which("unrar")
    if unrar:
        return [unrar, "x", "-y"]
    return None


def extract_windbg_rar(windbg_src: Path, out_dir: Path) -> None:
    """Extract doc/windbg64.part01.rar (multi-part) into out_dir."""
    part1 = windbg_src / "doc" / "windbg64.part01.rar"
    if not part1.exists():
        die(f"RAR part not found: {part1}")
    out_dir.mkdir(parents=True, exist_ok=True)
    ex = find_extractor()
    if ex is None:
        die("No RAR extractor found (install 7-Zip or WinRAR).")
    base = os.path.basename(ex[0]).lower()
    if "winrar" in base:
        cmd = ex + ["x", "-y", str(part1), str(out_dir) + os.sep]
    elif "7z" in base or "7za" in base:
        cmd = ex + [str(part1), f"-o{out_dir}"]
    else:  # unrar
        cmd = ex + [str(part1), str(out_dir) + os.sep]
    print(f"[*] extracting {part1.name} (+ parts) -> {out_dir}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        die(f"extraction failed:\n{r.stdout}\n{r.stderr}")


def locate(root: Path, names: list[str]) -> Path | None:
    """First file under root whose name (case-insensitive) is in names."""
    want = {n.lower() for n in names}
    if not root.exists():
        return None
    for p in root.rglob("*"):
        if p.is_file() and p.name.lower() in want:
            return p
    return None


# --------------------------------------------------------------------------- #
#  ntoseye bridge (Linux/macOS debugger; runs under WSL on Windows)
# --------------------------------------------------------------------------- #
def _wsl_available() -> bool:
    return shutil.which("wsl.exe") is not None or shutil.which("wsl") is not None


def detect_ntoseye() -> tuple[bool, str]:
    """Return (use_wsl, path); path is '' if ntoseye was not found."""
    native = shutil.which("ntoseye") or shutil.which("ntoseye.exe")
    if native:
        return False, native
    if _wsl_available():
        try:
            r = subprocess.run(
                ["wsl.exe", "-e", "bash", "-lic", "command -v ntoseye"],
                capture_output=True, text=True, timeout=25)
            lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
            if r.returncode == 0 and lines:
                return True, lines[-1]
        except Exception:
            pass
    return False, ""


def to_wsl_path(winpath: str) -> str:
    """Translate a Windows path to its WSL /mnt path (wslpath, then fallback)."""
    p = str(Path(winpath).resolve())
    try:
        r = subprocess.run(["wsl.exe", "wslpath", "-a", p],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    drive, rest = os.path.splitdrive(p)
    return f"/mnt/{drive[0].lower()}{rest.replace(chr(92), '/')}" if drive else p


def ntoseye_prefix(cfg: dict):
    """Return (argv_prefix, use_wsl) or (None, None) if ntoseye is unresolved."""
    path = cfg.get("ntoseye_path", "")
    if not path:
        return None, None
    if cfg.get("ntoseye_use_wsl"):
        return ["wsl.exe", "--", path], True
    return [path], False


def dokan_installed() -> bool:
    """True if a Dokany kernel driver (dokan2/1.sys) is present on the system."""
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    drivers = Path(sysroot) / "System32" / "drivers"
    return any((drivers / n).exists() for n in ("dokan2.sys", "dokan1.sys", "dokan.sys"))


def install_ntoseye_wsl() -> tuple[bool, str]:
    """Try to install ntoseye inside WSL (uv, then pipx, then cargo). Opt-in."""
    if not _wsl_available():
        print("[!] WSL introuvable : installation auto de ntoseye impossible.")
        return False, ""
    # Each attempt is a no-op unless its installer exists, then re-detect.
    attempts = [
        ("uv", "uv tool install ntoseye"),
        ("pipx", "pipx install ntoseye"),
        ("cargo", "cargo install ntoseye"),
    ]
    for tool, install_cmd in attempts:
        guard = f"command -v {tool} >/dev/null 2>&1 && {install_cmd}"
        print(f"[*] tentative via {tool} (WSL)...")
        try:
            r = subprocess.run(["wsl.exe", "-e", "bash", "-lic", guard], timeout=1800)
            if r.returncode == 0:
                break
        except Exception as e:
            print(f"[~] {tool} : {e}")
    return detect_ntoseye()


# --------------------------------------------------------------------------- #
#  Engine (hvlib.dll via the shipped hvlib.py wrapper)
# --------------------------------------------------------------------------- #
def load_hvlib(cfg: dict):
    """Import the shipped hvlib.py and instantiate it against hvlib.dll."""
    hvlib_py = cfg.get("hvlib_py")
    hvlib_dll = cfg.get("hvlib_dll")
    if not hvlib_py or not Path(hvlib_py).exists():
        die("hvlib.py not resolved. Run setup.")
    if not hvlib_dll or not Path(hvlib_dll).exists():
        die("hvlib.dll not resolved. Run setup.")
    # hvlib.dll loads its sibling dependencies from its own folder.
    os.add_dll_directory(str(Path(hvlib_dll).parent))
    # hvlib.hvlib(dll) also insists on finding hvlib.py next to the DLL
    # (LiveCloudKd ships them in different folders): stage a copy there.
    staged_py = Path(hvlib_dll).parent / "hvlib.py"
    if not staged_py.exists():
        try:
            shutil.copy2(hvlib_py, staged_py)
        except OSError as e:
            die(f"cannot copy hvlib.py next to hvlib.dll ({staged_py}): {e}")
    spec = importlib.util.spec_from_file_location("hvlib", hvlib_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    engine = mod.hvlib(hvlib_dll)
    return mod, engine


def engine_enum(cfg: dict):
    """Return (mod, engine, list_of_(id,name)) with partitions enumerated."""
    mod, engine = load_hvlib(cfg)
    ops = engine.GetPreferredSettings()
    ops.ReadMethod = int(cfg["read_method"])
    ops.WriteMethod = int(cfg["write_method"])
    ops.LogLevel = 1
    try:
        ok = engine.EnumPartitions(ops)
    except (OSError, ValueError):   # hvlib.py dereferences a NULL array without admin
        ok = False
    if not ok:
        die("EnumPartitions failed (need admin + running Hyper-V VM?).")
    vms = list(enumerate(engine.ArrayOfNames))
    return mod, engine, vms


# --------------------------------------------------------------------------- #
#  Commands
# --------------------------------------------------------------------------- #
def cmd_setup(args):
    cfg = default_config()
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    if args.livecloudkd:
        cfg["livecloudkd_dir"] = args.livecloudkd
    if args.windbg_src:
        cfg["windbg_src_dir"] = args.windbg_src

    lck = Path(cfg["livecloudkd_dir"])
    wbs = Path(cfg["windbg_src_dir"])

    # --- 1. engine files (shipped prebuilt in the SDK) ---------------------- #
    hvlib_dll = locate(lck, ["hvlib.dll"])
    hvlib_py = locate(lck, ["hvlib.py"])
    hvmm_sys = locate(lck, ["hvmm.sys"])
    if not hvlib_dll:
        die(f"hvlib.dll not found under {lck}")
    if not hvlib_py:
        die(f"hvlib.py not found under {lck}")
    cfg["hvlib_dll"] = str(hvlib_dll)
    cfg["hvlib_py"] = str(hvlib_py)
    cfg["hvlib_dir"] = str(hvlib_dll.parent)
    print(f"[+] engine: hvlib.dll  {hvlib_dll}")
    print(f"[+] engine: hvlib.py   {hvlib_py}")
    print(f"[+] engine: hvmm.sys   {hvmm_sys if hvmm_sys else '(not found)'}")

    # --- 2. debugger (extract WinDbg from the RAR, unless already) ---------- #
    windbg_out = HERE / "windbg64"
    windbg_exe = locate(windbg_out, ["windbg.exe"])
    if windbg_exe is None:
        if args.no_extract:
            print("[*] --no-extract: skipping RAR extraction")
        else:
            extract_windbg_rar(wbs, windbg_out)
            windbg_exe = locate(windbg_out, ["windbg.exe"])
    kd_exe = locate(windbg_out, ["kd.exe"])
    # allow pointing at an existing WinDbg install instead
    if args.windbg_dir:
        d = Path(args.windbg_dir)
        windbg_exe = locate(d, ["windbg.exe"]) or windbg_exe
        kd_exe = locate(d, ["kd.exe"]) or kd_exe
    if windbg_exe:
        cfg["windbg_exe"] = str(windbg_exe)
        cfg["windbg_dir"] = str(windbg_exe.parent)
    if kd_exe:
        cfg["kd_exe"] = str(kd_exe)
        cfg["windbg_dir"] = cfg["windbg_dir"] or str(kd_exe.parent)
    print(f"[+] debugger: windbg.exe {windbg_exe if windbg_exe else '(not found)'}")
    print(f"[+] debugger: kd.exe     {kd_exe if kd_exe else '(not found)'}")

    # --- 3. LiveCloudKd.exe launcher (optional; source ships none) ---------- #
    lck_exe = locate(lck, ["LiveCloudKd.exe"])
    # An explicitly supplied prebuilt exe (official release) wins over auto-locate.
    if args.livecloudkd_exe:
        p = Path(args.livecloudkd_exe)
        if p.exists():
            lck_exe = p
        else:
            print(f"[~] --livecloudkd-exe introuvable: {p}")
    cfg["livecloudkd_exe"] = str(lck_exe) if lck_exe else ""
    if lck_exe:
        print(f"[+] launcher: LiveCloudKd.exe {lck_exe}")
    else:
        print("[+] launcher: LiveCloudKd.exe (absent - mode moteur seul ; "
              "l'arbre source ne compile pas, voir README ; "
              "pointe la release prébuild avec --livecloudkd-exe)")

    # --- 3b. ntoseye (offline dump analysis; detected on PATH or in WSL) ---- #
    if args.ntoseye_src:
        cfg["ntoseye_src_dir"] = args.ntoseye_src
    if not args.no_ntoseye:
        use_wsl, npath = detect_ntoseye()
        if not npath and args.install_ntoseye:
            use_wsl, npath = install_ntoseye_wsl()
        cfg["ntoseye_use_wsl"] = use_wsl
        cfg["ntoseye_path"] = npath
        if npath:
            print(f"[+] ntoseye: {'(WSL) ' if use_wsl else ''}{npath}")
        elif args.install_ntoseye:
            print("[~] ntoseye: toujours introuvable après tentative d'installation")
        else:
            print("[~] ntoseye: non détecté (installe-le sous WSL, ou "
                  "relance setup avec --install-ntoseye ; voir README)")

    # --- 4. stage engine files next to the debugger (README requirement) ---- #
    if cfg["windbg_dir"] and not args.no_stage:
        dst = Path(cfg["windbg_dir"])
        for src in [hvlib_dll, hvmm_sys, lck_exe]:
            if src and src.exists():
                try:
                    shutil.copy2(src, dst / src.name)
                    print(f"[+] staged {src.name} -> {dst}")
                except Exception as e:
                    print(f"[~] could not stage {src.name}: {e}")

    # --- 3c. MemProcFS + Dokany (mount memory as a filesystem) ------------- #
    if args.memprocfs_src:
        cfg["memprocfs_src_dir"] = args.memprocfs_src
    mp_exe = None
    if args.memprocfs_exe and Path(args.memprocfs_exe).exists():
        mp_exe = Path(args.memprocfs_exe)
    else:
        found = shutil.which("MemProcFS.exe") or shutil.which("memprocfs")
        if found:
            mp_exe = Path(found)
        elif cfg.get("memprocfs_exe") and Path(cfg["memprocfs_exe"]).exists():
            mp_exe = Path(cfg["memprocfs_exe"])
        else:  # a prebuilt MemProcFS.exe dropped into the source tree
            mp_exe = locate(Path(cfg["memprocfs_src_dir"]), ["MemProcFS.exe"])
    if mp_exe:
        cfg["memprocfs_exe"] = str(mp_exe)
        cfg["memprocfs_dir"] = str(mp_exe.parent)
        print(f"[+] MemProcFS: {mp_exe}")
    else:
        print("[~] MemProcFS.exe non détecté (release officielle ou build ; "
              "--memprocfs-exe <chemin>)")

    if args.leechcore_src:
        cfg["leechcore_src_dir"] = args.leechcore_src
    # leechcore.dll ships beside MemProcFS.exe; also look in the LeechCore tree.
    lc_dll = (locate(Path(cfg["memprocfs_dir"]), ["leechcore.dll"]) if cfg.get("memprocfs_dir") else None)
    if not lc_dll:
        lc_dll = locate(Path(cfg["leechcore_src_dir"]), ["leechcore.dll"])
    cfg["leechcore_dll"] = str(lc_dll) if lc_dll else ""
    print(f"[+] LeechCore: {lc_dll if lc_dll else '(leechcore.dll non trouvé)'}")

    hvmm_dll = locate(lck, ["leechcore_device_hvmm.dll"])
    cfg["leechcore_hvmm_dll"] = str(hvmm_dll) if hvmm_dll else ""
    print(f"[+] plugin hvmm: {hvmm_dll if hvmm_dll else '(non compilé - mode live hvmm indisponible ; voir README)'}")

    cfg["dokan_installed"] = dokan_installed()
    print(f"[+] Dokany: {'installé' if cfg['dokan_installed'] else 'ABSENT (installer DokanSetup.exe signé)'}")

    # Stage the hvmm plugin + engine next to MemProcFS.exe (device 'hvmm').
    if mp_exe and hvmm_dll and not args.no_stage:
        for src in [hvmm_dll, hvlib_dll, hvmm_sys]:
            if src and src.exists():
                try:
                    shutil.copy2(src, mp_exe.parent / src.name)
                    print(f"[+] staged {src.name} -> {mp_exe.parent}")
                except Exception as e:
                    print(f"[~] could not stage {src.name}: {e}")

    save_config(cfg)
    print("\nNext:  python hvkdfusion.py list      (needs admin + Hyper-V VM)")


def cmd_config(args):
    cfg = load_config()
    print(json.dumps(cfg, indent=2))


def cmd_list(args):
    cfg = load_config()
    _, engine, vms = engine_enum(cfg)
    if not vms:
        print("No Hyper-V partitions found.")
    else:
        print(f"{'ID':>3}  VM name")
        print("-" * 40)
        for vid, name in vms:
            print(f"{vid:>3}  {name}")
    engine.cleanup()


def _parse_int(s: str) -> int:
    return int(s, 0)  # accepts 0x.. and decimal


def cmd_read(args):
    cfg = load_config()
    mod, engine, vms = engine_enum(cfg)
    if args.vm >= len(vms):
        die(f"VM id {args.vm} out of range (have {len(vms)}).")
    handle = engine.SelectPartition(args.vm)
    addr = _parse_int(args.addr)
    size = _parse_int(args.size)
    if args.virtual:
        buf = engine.ReadVirtualMemoryBlock(handle, addr, size)
    else:
        buf = engine.ReadPhysicalMemoryBlock(handle, addr, size)
    if buf is None:
        die("read failed.")
    data = bytes(buf)
    if args.out:
        Path(args.out).write_bytes(data)
        print(f"[+] wrote {len(data)} bytes -> {args.out}")
    else:
        engine.PrintHex(buf)
    engine.cleanup()


def cmd_write(args):
    cfg = load_config()
    mod, engine, vms = engine_enum(cfg)
    if args.vm >= len(vms):
        die(f"VM id {args.vm} out of range (have {len(vms)}).")
    handle = engine.SelectPartition(args.vm)
    addr = _parse_int(args.addr)
    if args.file:
        data = Path(args.file).read_bytes()
    elif args.data:
        data = bytes.fromhex(args.data.replace(" ", ""))
    else:
        die("provide --data <hex> or --file <path>")
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    if args.virtual:
        ok = engine.WriteVirtualMemoryBlock(handle, addr, buf)
    else:
        ok = engine.WritePhysicalMemoryBlock(handle, addr, buf)
    print(f"[{'+' if ok else '!'}] wrote {len(data)} bytes @ {hex(addr)} "
          f"({'virtual' if args.virtual else 'physical'}): {bool(ok)}")
    engine.cleanup()


def cmd_dump(args):
    """Dump guest physical memory. Prefer LiveCloudKd.exe; else engine loop."""
    cfg = load_config()
    if cfg["livecloudkd_exe"] and args.type in ("linear", "crash", "raw"):
        action = {"linear": ACTION_DUMP_LINEAR,
                  "crash": ACTION_DUMP_CRASH,
                  "raw": ACTION_DUMP_RAW}[args.type]
        argv = [cfg["livecloudkd_exe"], "/a", str(action), "/n", str(args.vm),
                "/m", str(cfg["read_method"]), "/b"]
        if args.out:
            argv += ["/o", args.out]
        print("[*] launching:", " ".join(argv))
        return sys.exit(subprocess.run(argv).returncode)

    # engine fallback: linear physical dump via ReadPhysicalMemory loop
    if args.type != "linear":
        die(f"'{args.type}' dump needs LiveCloudKd.exe (not built). "
            f"Use --type linear for the engine dump, or build LiveCloudKd.exe.")
    if not args.out:
        die("engine dump needs --out <path>")
    engine_linear_dump(cfg, args.vm, args.out)


def _max_phys_page_class(mod) -> int:
    """Resolve the 'max physical page' info-class across SDK wrapper versions."""
    for cls_name, member in (("HvmmInformationClass", "InfoMmMaximumPhysicalPage"),
                             ("HvddInformationClass", "HvddMmMaximumPhysicalPage")):
        cls = getattr(mod, cls_name, None)
        if cls is not None and hasattr(cls, member):
            return int(getattr(cls, member))
    return 6  # stable value in both SDK versions


def engine_linear_dump(cfg: dict, vm: int, out: str) -> str:
    """Write a linear (raw) physical memory dump of VM `vm` to `out`."""
    mod, engine, vms = engine_enum(cfg)
    if vm >= len(vms):
        die(f"VM id {vm} out of range (have {len(vms)}).")
    handle = engine.SelectPartition(vm)
    max_page = engine.GetData(handle, _max_phys_page_class(mod))
    try:
        max_page = int(max_page)
    except Exception:
        die("could not read HvddMmMaximumPhysicalPage.")
    total = (max_page + 1) * 0x1000
    chunk = 0x100000  # 1 MiB
    print(f"[*] linear dump: {total / (1024 * 1024):.0f} MiB -> {out}")
    written = 0
    with open(out, "wb") as f:
        addr = 0
        while addr < total:
            n = min(chunk, total - addr)
            buf = engine.ReadPhysicalMemoryBlock(handle, addr, n)
            f.write(bytes(buf) if buf is not None else b"\x00" * n)
            written += n
            addr += n
            if (addr // chunk) % 64 == 0:
                print(f"\r    {written / (1024 * 1024):8.0f} MiB", end="", flush=True)
    print(f"\n[+] done: {written} bytes -> {out}")
    engine.cleanup()
    return out


def cmd_wineye(args):
    """Launch the native-Windows passive WinDbg-style REPL (wineye.py)."""
    wineye_py = HERE / "wineye.py"
    if not wineye_py.exists():
        die(f"wineye.py introuvable à {wineye_py}")
    import importlib.util
    spec = importlib.util.spec_from_file_location("wineye", wineye_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main(args.args)


def cmd_mount(args):
    """Mount memory as a filesystem with MemProcFS (needs Dokany).

    Three sources fuse LiveCloudKd's engine with MemProcFS:
      --dump <file>  : mount an existing dump (.raw / .dmp)
      --from-vm N    : linear-dump VM N via the engine, then mount the raw file
      --live         : mount a live Hyper-V VM through the leechcore 'hvmm'
                       device (needs leechcore_device_hvmm.dll in the MemProcFS
                       folder; setup stages it when available)
    """
    cfg = load_config()
    mp = cfg.get("memprocfs_exe")
    if not mp or not Path(mp).exists():
        die("MemProcFS.exe introuvable. Récupère la release officielle "
            "(github.com/ufrisk/MemProcFS/releases) puis relance "
            "setup --memprocfs-exe <chemin>.")
    if not cfg.get("dokan_installed") and not dokan_installed():
        print("[!] Dokany absent (dokan2.sys) : le montage échouera. Installe la "
              "release signée DokanSetup.exe : github.com/dokan-dev/dokany/releases")

    if args.live:
        if not cfg.get("leechcore_hvmm_dll") and not locate(Path(cfg["memprocfs_dir"] or "."), ["leechcore_device_hvmm.dll"]):
            print("[!] leechcore_device_hvmm.dll absent du dossier MemProcFS : le "
                  "device 'hvmm' ne marchera pas. Récupère la release "
                  "'leechcore_hyperv_plugin' de LiveCloudKd et place le .dll "
                  "(+ hvlib.dll, hvmm.sys) à côté de MemProcFS.exe.")
        device = "hvmm"
    elif args.dump:
        if not Path(args.dump).exists():
            die(f"dump introuvable: {args.dump}")
        device = args.dump
    elif args.from_vm is not None:
        out = args.out or str(HERE / f"vm{args.from_vm}.raw")
        device = engine_linear_dump(cfg, args.from_vm, out)
    elif args.device:
        device = args.device  # any LeechCore device: fpga, pmem, vmware://, ...
    else:
        die("choisir --dump <fichier>, --from-vm <N>, --live, ou --device <leechcore>")

    argv = [mp, "-device", device]
    if args.letter:
        argv += ["-mount", args.letter]
    if args.forensic:
        argv += ["-forensic", "1"]
    if args.verbose:
        argv.append("-v")
    if args.extra:
        argv += args.extra
    print("[*] lancement MemProcFS:", " ".join(argv))
    print("    (fermer MemProcFS pour démonter ; Ctrl+C dans sa fenêtre)")
    sys.exit(subprocess.run(argv).returncode)


def cmd_debug(args):
    """Attach a debugger to the VM via the LiveCloudKd.exe launcher."""
    cfg = load_config()
    if not cfg["livecloudkd_exe"]:
        die("LiveCloudKd.exe is not available (source tree ships no prebuilt "
            "binary). Build LiveCloudKd\\LiveCloudKd.sln, re-run setup, or use "
            "the engine commands (list/read/write/dump).")
    engine_choice = args.engine
    action = ACTION_EXDI_WINDBG if engine_choice == "exdi" else ACTION_LIVE_KD
    argv = [cfg["livecloudkd_exe"], "/a", str(action), "/n", str(args.vm),
            "/m", str(cfg["read_method"])]
    if engine_choice == "windbg":
        argv.append("/w")  # WinDbg instead of kd
    if cfg["windbg_dir"]:
        argv += ["/y", cfg["windbg_dir"]]
    if args.pause:
        argv.append("/p")
    if args.verbose:
        argv.append("/v")
    print("[*] launching:", " ".join(argv))
    sys.exit(subprocess.run(argv).returncode)


def cmd_resume(args):
    cfg = load_config()
    # Prefer engine ControlVmState; fall back to LiveCloudKd.exe /a 6.
    try:
        mod, engine, vms = engine_enum(cfg)
        if args.vm >= len(vms):
            die(f"VM id {args.vm} out of range (have {len(vms)}).")
        handle = engine.SelectPartition(args.vm)
        ok = engine.hvlib.SdkControlVmState(
            ctypes.c_uint64(handle),
            ctypes.c_uint32(mod.VmStateAction.ResumeVm),
            ctypes.c_uint32(mod.SuspendResumeMethod.SuspendResumePowershell),
            ctypes.c_bool(True))
        print(f"[{'+' if ok else '!'}] resume VM {args.vm}: {bool(ok)}")
        engine.cleanup()
    except SystemExit:
        raise
    except Exception as e:
        if cfg["livecloudkd_exe"]:
            argv = [cfg["livecloudkd_exe"], "/a", str(ACTION_RESUME),
                    "/n", str(args.vm), "/b"]
            print(f"[*] engine path failed ({e}); launching:", " ".join(argv))
            sys.exit(subprocess.run(argv).returncode)
        die(f"resume failed: {e}")


def cmd_ntoseye(args):
    """Analyse a VM dump with ntoseye (REPL / MCP / DAP / gdbserver).

    ntoseye is a WinDbg-like debugger for Linux/macOS, so on Windows it runs
    under WSL. It reads Windows .dmp files; the fusion path is therefore:
    HvKdFusion produces a Microsoft crash dump of the live Hyper-V VM, then
    ntoseye analyses it offline. (The engine's 'linear' dump is RAW and is not
    a .dmp, so ntoseye cannot read it -- use a crash dump.)
    """
    cfg = load_config()

    # Resolve the ntoseye invocation (config/detection, or an explicit override).
    if args.ntoseye:
        prefix, use_wsl = ((["wsl.exe", "--", args.ntoseye], True)
                           if args.wsl else ([args.ntoseye], False))
    else:
        prefix, use_wsl = ntoseye_prefix(cfg)
    if prefix is None:
        if not args.print_cmd:
            die("ntoseye introuvable. Installe-le sous WSL "
                "(ex. 'uv tool install ntoseye'), relance setup, "
                "ou passe --ntoseye <chemin> [--wsl].")
        prefix, use_wsl = ["ntoseye"], False  # placeholder for --print-cmd

    # Resolve the dump: an existing .dmp, or produce a crash dump from a VM.
    dump = args.dump
    if not dump:
        if args.vm is None:
            die("fournir --dump <fichier.dmp> ou --vm <N> (pour produire un crash dump).")
        if not cfg["livecloudkd_exe"]:
            die("Produire un crash dump nécessite LiveCloudKd.exe (non compilé). "
                "Compile LiveCloudKd.sln puis relance setup, ou passe --dump <existant>. "
                "Le dump 'linear' du moteur est RAW et n'est pas lisible par ntoseye.")
        dump = args.out or str(HERE / f"vm{args.vm}.dmp")
        argv = [cfg["livecloudkd_exe"], "/a", str(ACTION_DUMP_CRASH),
                "/n", str(args.vm), "/m", str(cfg["read_method"]), "/o", dump, "/b"]
        print("[*] crash dump:", " ".join(argv))
        if subprocess.run(argv).returncode != 0 or not Path(dump).exists():
            die("génération du crash dump échouée.")

    dump_arg = to_wsl_path(dump) if (use_wsl and dump) else dump

    # Subcommand per mode (repl = no subcommand).
    sub: list[str] = []
    if args.mode == "mcp":
        sub = ["mcp"] + (["--http", args.http] if args.http else [])
    elif args.mode == "dap":
        sub = ["dap"] + (["--port", str(args.port)] if args.port else [])
    elif args.mode == "gdbserver":
        sub = ["gdbserver"]

    cmd = list(prefix) + sub + ["--dump", dump_arg]
    for c in (args.command or []):
        cmd += ["-c", c]
    if args.batch:
        cmd.append("--batch")
    if args.extra:
        cmd += args.extra

    if args.print_cmd:
        print(" ".join(cmd))
        return
    print("[*] lancement ntoseye:", " ".join(cmd))
    sys.exit(subprocess.run(cmd).returncode)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def hyperv_present() -> bool:
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    s32 = Path(sysroot) / "System32"
    return (s32 / "vmms.exe").exists() or (s32 / "vmcompute.exe").exists()


def _exists(p: str | None) -> bool:
    return bool(p) and Path(p).exists()


def _siblings_present(exe: str | None, names: list[str]) -> list[str]:
    """Return which of `names` are missing next to `exe` (empty = all present)."""
    if not _exists(exe):
        return names[:]
    d = Path(exe).parent
    return [n for n in names if not (d / n).exists()]


def cmd_check(args):
    """End-to-end readiness report (static; no DLL load, no admin needed)."""
    cfg = load_config()
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    s32 = Path(sysroot) / "System32"

    rows: list[tuple[str, str, str]] = []

    def add(name, ok, detail, soft=False):
        mark = "[OK]" if ok else ("[~~]" if soft else "[!!]")
        rows.append((mark, name, detail))

    # Base
    add("Python", True, f"{sys.version.split()[0]}")
    add("Admin (session courante)", is_admin(),
        "élevé" if is_admin() else "non élevé - requis pour les ops mémoire", soft=not is_admin())
    add("Hyper-V (hôte)", hyperv_present(),
        "vmms/vmcompute présents" if hyperv_present() else "non détecté", soft=True)
    add("Runtime VC++ (vcruntime140)", (s32 / "vcruntime140.dll").exists(),
        "présent" if (s32 / "vcruntime140.dll").exists() else "manquant (requis par hvlib/MemProcFS)")

    # Moteur
    add("Moteur hvlib.dll", _exists(cfg.get("hvlib_dll")), cfg.get("hvlib_dll") or "absent")
    add("Moteur hvlib.py", _exists(cfg.get("hvlib_py")), cfg.get("hvlib_py") or "absent")
    add("Driver hvmm.sys", _exists(str(Path(cfg.get("hvlib_dir", ".")) / "hvmm.sys")),
        "présent" if _exists(str(Path(cfg.get("hvlib_dir", ".")) / "hvmm.sys")) else "absent", soft=True)

    # Débogueurs
    add("WinDbg (windbg.exe)", _exists(cfg.get("windbg_exe")), cfg.get("windbg_exe") or "absent")
    miss = _siblings_present(cfg.get("livecloudkd_exe"), ["hvlib.dll"])
    add("Lanceur LiveCloudKd.exe", _exists(cfg.get("livecloudkd_exe")) and not miss,
        (cfg.get("livecloudkd_exe") or "absent (mode moteur seul)") +
        (f" - manque {miss} à côté" if miss and _exists(cfg.get('livecloudkd_exe')) else ""),
        soft=not _exists(cfg.get("livecloudkd_exe")))
    npath = cfg.get("ntoseye_path")
    add("ntoseye", _exists(npath) or bool(npath),
        (("(WSL) " if cfg.get("ntoseye_use_wsl") else "") + npath) if npath else "non détecté",
        soft=not npath)

    # MemProcFS / LeechCore / Dokany
    mp = cfg.get("memprocfs_exe")
    mp_miss = _siblings_present(mp, ["vmm.dll", "leechcore.dll"])
    add("MemProcFS.exe", _exists(mp) and not mp_miss,
        (mp or "absent") + (f" - manque {mp_miss}" if mp_miss and _exists(mp) else ""),
        soft=not _exists(mp))
    # plugin live hvmm : présent dans le dossier MemProcFS, avec ses deps
    live_ok = False
    live_detail = "non compilé / absent du dossier MemProcFS"
    if _exists(mp):
        d = Path(mp).parent
        plug = d / "leechcore_device_hvmm.dll"
        if plug.exists():
            dep_miss = [n for n in ("hvlib.dll", "hvmm.sys", "leechcore.dll") if not (d / n).exists()]
            live_ok = not dep_miss
            live_detail = "en place" if live_ok else f"présent mais manque {dep_miss}"
    add("Plugin live hvmm", live_ok, live_detail, soft=not live_ok)
    dokan = cfg.get("dokan_installed") or dokan_installed()
    add("Dokany (dokan2.sys)", dokan,
        "installé" if dokan else "ABSENT - installer DokanSetup.exe signé")

    # --- rendu du tableau ---
    print("\n=== HvKdFusion : préparation ===")
    w = max(len(n) for _, n, _ in rows)
    for mark, name, detail in rows:
        print(f"  {mark}  {name.ljust(w)}  {detail}")

    # --- fonctionnalités prêtes ---
    eng = _exists(cfg.get("hvlib_dll")) and _exists(cfg.get("hvlib_py"))
    feat = [
        ("Moteur (list/read/write/dump linear/resume)", eng, "hvlib + admin + VM Hyper-V"),
        ("Lanceur (debug / dump crash)", _exists(cfg.get("livecloudkd_exe")) and _exists(cfg.get("windbg_exe")), "LiveCloudKd.exe + WinDbg"),
        ("ntoseye (analyse de dump)", bool(cfg.get("ntoseye_path")), "ntoseye (WSL)"),
        ("Montage dump / from-vm", _exists(mp) and not mp_miss and dokan, "MemProcFS + Dokany"),
        ("Montage live (hvmm)", live_ok and dokan, "plugin hvmm + Dokany"),
        ("wineye REPL (dump, sans Dokany)", _exists(mp) and not mp_miss, "vmm.dll"),
        ("wineye REPL (live hvmm)", live_ok, "vmm.dll + plugin hvmm + admin"),
    ]
    print("\n=== Fonctionnalités ===")
    wf = max(len(n) for n, _, _ in feat)
    for name, ok, need in feat:
        print(f"  {'[OK]' if ok else '[  ]'}  {name.ljust(wf)}  ({need})")

    # --- test profond optionnel : exécute réellement MemProcFS.exe -h ---
    if args.deep and _exists(mp):
        print("\n=== --deep : exécution de MemProcFS.exe ===")
        try:
            r = subprocess.run([mp], capture_output=True, text=True, timeout=30)
            lines = [l.strip() for l in (r.stdout or r.stderr).splitlines() if l.strip()]
            ok = any("MemProcFS" in l for l in lines[:3])
            print(f"  {'[OK]' if ok else '[!!]'}  MemProcFS.exe se lance : {lines[0] if lines else '(pas de sortie)'}")
        except Exception as e:
            print(f"  [!!]  échec du lancement : {e}")

    print()


# --------------------------------------------------------------------------- #
#  Interactive menu
# --------------------------------------------------------------------------- #
def _ask(prompt: str, default: str = "") -> str:
    try:
        s = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return s or default


def _run(func, **kw):
    """Dispatch a cmd_* function with a Namespace, surviving its sys.exit()."""
    try:
        func(types.SimpleNamespace(**kw))
    except SystemExit as e:
        if e.code not in (0, None):
            print(f"[~] commande terminée (code {e.code})")
    except Exception as e:  # keep the menu alive on engine errors
        print(f"[!] erreur : {e}")


def cmd_menu(args):
    cfg = load_config()
    # Enumerate once just to display the VM list, then release the engine.
    mod, engine, vms = engine_enum(cfg)
    engine.cleanup()
    if not vms:
        die("Aucune partition Hyper-V trouvée (admin + VM en cours requis).")

    print("\n=== HvKdFusion ===")
    print("VM Hyper-V disponibles :")
    for vid, name in vms:
        print(f"   [{vid}] {name}")
    sel = _ask(f"\nChoisir une VM (0-{len(vms) - 1}) : ")
    try:
        vm = int(sel, 0)
    except ValueError:
        die("sélection invalide.")
    if not (0 <= vm < len(vms)):
        die("VM hors plage.")
    vm_name = vms[vm][1]

    actions = {
        "1": "Déboguer avec WinDbg",
        "2": "Déboguer avec kd",
        "3": "Déboguer via plugin EXDI",
        "4": "Dump mémoire linéaire -> fichier",
        "5": "Lire mémoire",
        "6": "Écrire mémoire",
        "7": "Reprendre (resume) la VM",
        "8": "Analyser un dump avec ntoseye",
        "9": "Monter la mémoire (MemProcFS)",
        "0": "Quitter",
    }
    while True:
        print(f"\n--- VM [{vm}] {vm_name} ---")
        for k in sorted(actions):
            print(f"   {k}. {actions[k]}")
        choice = _ask("Action : ")
        if choice == "0":
            return
        elif choice == "1":
            _run(cmd_debug, vm=vm, engine="windbg", pause=False, verbose=False)
        elif choice == "2":
            _run(cmd_debug, vm=vm, engine="kd", pause=False, verbose=False)
        elif choice == "3":
            _run(cmd_debug, vm=vm, engine="exdi", pause=False, verbose=False)
        elif choice == "4":
            out = _ask("Fichier de sortie : ")
            if out:
                _run(cmd_dump, vm=vm, type="linear", out=out)
        elif choice == "5":
            addr = _ask("Adresse (0x..) : ")
            size = _ask("Taille (0x.. ou déc) [0x100] : ", "0x100")
            virt = _ask("Adresse virtuelle ? (o/N) : ").lower().startswith("o")
            out = _ask("Écrire dans un fichier ? (vide = hexdump) : ")
            if addr:
                _run(cmd_read, vm=vm, addr=addr, size=size,
                     virtual=virt, out=out or None)
        elif choice == "6":
            addr = _ask("Adresse (0x..) : ")
            data = _ask("Octets hex (ex. 9090cc) : ")
            virt = _ask("Adresse virtuelle ? (o/N) : ").lower().startswith("o")
            if addr and data:
                print("[!] écriture mémoire dans le guest — confirmer")
                if _ask("Confirmer ? (o/N) : ").lower().startswith("o"):
                    _run(cmd_write, vm=vm, addr=addr, data=data,
                         file=None, virtual=virt)
        elif choice == "7":
            _run(cmd_resume, vm=vm)
        elif choice == "8":
            dmp = _ask("Fichier .dmp (vide = crash dump de la VM, LiveCloudKd.exe requis) : ")
            mode = _ask("Mode [repl/mcp/dap/gdbserver] (repl) : ", "repl")
            _run(cmd_ntoseye, vm=(None if dmp else vm), dump=(dmp or None),
                 out=None, mode=mode, command=None, batch=False, http=None,
                 port=None, ntoseye=None, wsl=False, print_cmd=False, extra=[])
        elif choice == "9":
            src = _ask("Source [1=dump VM (moteur), 2=dump existant, 3=live hvmm] (1) : ", "1")
            letter = _ask("Lettre de lecteur (vide = M par défaut) : ")
            kw = dict(dump=None, from_vm=None, live=False, out=None,
                      letter=(letter or None), forensic=False, verbose=False, extra=[])
            if src == "2":
                kw["dump"] = _ask("Chemin du dump (.raw/.dmp) : ") or None
            elif src == "3":
                kw["live"] = True
            else:
                kw["from_vm"] = vm
            _run(cmd_mount, **kw)
        else:
            print("choix inconnu.")


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hvkdfusion",
        description="Unified Hyper-V live-kernel-debug orchestrator "
                    "(LiveCloudKd engine + WinDbg debugger).")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="resolve + stage toolchains, write config.json")
    s.add_argument("--livecloudkd", help="LiveCloudKd source dir")
    s.add_argument("--windbg-src", help="windbg-master source dir")
    s.add_argument("--windbg-dir", help="use an existing WinDbg install dir instead of the RAR")
    s.add_argument("--no-extract", action="store_true", help="do not extract the WinDbg RAR")
    s.add_argument("--no-stage", action="store_true", help="do not copy engine files next to WinDbg")
    s.add_argument("--ntoseye-src", help="ntoseye source dir")
    s.add_argument("--no-ntoseye", action="store_true", help="skip ntoseye detection")
    s.add_argument("--install-ntoseye", action="store_true",
                   help="if ntoseye is missing, try to install it in WSL (uv/pipx/cargo)")
    s.add_argument("--livecloudkd-exe", help="path to a prebuilt/built LiveCloudKd.exe")
    s.add_argument("--memprocfs-exe", help="path to MemProcFS.exe")
    s.add_argument("--memprocfs-src", help="MemProcFS source dir")
    s.add_argument("--leechcore-src", help="LeechCore source dir")
    s.set_defaults(func=cmd_setup)

    sub.add_parser("config", help="print resolved config").set_defaults(func=cmd_config)
    s = sub.add_parser("check", help="tableau de préparation de bout en bout")
    s.add_argument("--deep", action="store_true", help="exécute réellement MemProcFS.exe -h")
    s.set_defaults(func=cmd_check)
    sub.add_parser("list", help="enumerate Hyper-V VMs").set_defaults(func=cmd_list)
    sub.add_parser("menu", help="interactive menu (VM + action)").set_defaults(func=cmd_menu)

    s = sub.add_parser("debug", help="attach WinDbg/kd to a VM")
    s.add_argument("--vm", type=int, required=True, help="VM id (from list)")
    s.add_argument("--engine", choices=["kd", "windbg", "exdi"], default="windbg")
    s.add_argument("--pause", action="store_true")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_debug)

    s = sub.add_parser("dump", help="dump guest physical memory")
    s.add_argument("--vm", type=int, required=True)
    s.add_argument("--type", choices=["linear", "crash", "raw"], default="linear")
    s.add_argument("--out", help="output file")
    s.set_defaults(func=cmd_dump)

    s = sub.add_parser("read", help="read guest memory")
    s.add_argument("--vm", type=int, required=True)
    s.add_argument("--addr", required=True, help="address (0x.. or decimal)")
    s.add_argument("--size", required=True, help="bytes (0x.. or decimal)")
    s.add_argument("--virtual", action="store_true", help="virtual address (default physical)")
    s.add_argument("--out", help="write to file instead of hexdump")
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("write", help="write guest memory")
    s.add_argument("--vm", type=int, required=True)
    s.add_argument("--addr", required=True)
    s.add_argument("--data", help="hex bytes, e.g. 90909090")
    s.add_argument("--file", help="file whose bytes to write")
    s.add_argument("--virtual", action="store_true")
    s.set_defaults(func=cmd_write)

    s = sub.add_parser("resume", help="resume a paused VM")
    s.add_argument("--vm", type=int, required=True)
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("ntoseye",
                       help="analyse a VM dump with ntoseye (REPL/MCP/DAP/gdbserver)")
    s.add_argument("--vm", type=int, help="VM id -> produce a crash dump first (needs LiveCloudKd.exe)")
    s.add_argument("--dump", help="existing .dmp file to analyse")
    s.add_argument("--out", help="path for the crash dump to generate (with --vm)")
    s.add_argument("--mode", choices=["repl", "mcp", "dap", "gdbserver"], default="repl")
    s.add_argument("-c", "--command", action="append", help="ntoseye command to run after attach (repeatable)")
    s.add_argument("--batch", action="store_true", help="quit after -c commands")
    s.add_argument("--http", help="HTTP address for mcp mode (e.g. 127.0.0.1:8080)")
    s.add_argument("--port", type=int, help="TCP port for dap mode")
    s.add_argument("--ntoseye", help="path to the ntoseye executable (else config/detection)")
    s.add_argument("--wsl", action="store_true", help="invoke --ntoseye through WSL")
    s.add_argument("--print-cmd", action="store_true", help="print the command without running it")
    s.add_argument("extra", nargs="*", help="extra ntoseye arguments")
    s.set_defaults(func=cmd_ntoseye)

    s = sub.add_parser("mount",
                       help="monter la mémoire comme système de fichiers (MemProcFS + Dokany)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--dump", help="monter un dump existant (.raw/.dmp)")
    g.add_argument("--from-vm", type=int, help="dump linéaire de la VM N (moteur) puis monter")
    g.add_argument("--live", action="store_true", help="VM Hyper-V live via le device 'hvmm'")
    g.add_argument("--device", help="device LeechCore brut (fpga, pmem, vmware://..., etc.)")
    s.add_argument("--out", help="chemin du .raw à générer (avec --from-vm)")
    s.add_argument("--letter", help="lettre de lecteur (ex. M)")
    s.add_argument("--forensic", action="store_true", help="activer le mode forensic")
    s.add_argument("--verbose", action="store_true", help="sortie verbeuse (-v)")
    s.add_argument("extra", nargs="*", help="arguments MemProcFS supplémentaires")
    s.set_defaults(func=cmd_mount)

    s = sub.add_parser("wineye",
                       help="REPL natif Windows façon WinDbg (mémoire passive, sans Dokany/WSL)")
    s.add_argument("args", nargs=argparse.REMAINDER,
                   help="arguments wineye (--dump/--device/--live/--from-vm, -c, --json, ...)")
    s.set_defaults(func=cmd_wineye)

    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    # argparse.REMAINDER does not capture a remainder that starts with an
    # option ("wineye --dump x.raw" fails): hand everything after "wineye"
    # over to wineye's own parser unchanged.
    if argv[:1] == ["wineye"]:
        cmd_wineye(argparse.Namespace(args=argv[1:]))
        return
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
