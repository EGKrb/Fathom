#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wineye - a native-Windows, passive, WinDbg-style memory inspector.

This is the Windows-native counterpart to ntoseye's *memory backend*: it
inspects a Windows target from memory alone (a crash/raw dump, or a live
Hyper-V VM), with no debug transport in the guest and the target unaware it is
being looked at. It drives MemProcFS's vmm.dll directly through ctypes, so it
needs no WSL, no pip packages and no Python extension - just the vmm.dll we
built (plus leechcore.dll beside it, and for live VMs the hvmm plugin).

What it is NOT: it is not a full re-implementation of ntoseye. Passive memory
gives no run control (no stepping, no breakpoints, no register writes) - the
same limits ntoseye states for its `memory` backend. For run control on
Windows, use WinDbg/kd via `hvkdfusion.py debug`.

Commands (WinDbg-flavoured):
    ps                     list processes
    !process [pid]         detailed info for a process (default: current)
    .pid <n|-1>            set process context (-1 = physical memory)
    lm [pid]               list modules of a process (default: current)
    x <module>!<symbol>    resolve a symbol via PDB (e.g. x nt!PsInitialSystemProcess)
    db|dw|dd|dq <addr> [L<n>]   hex dump (bytes/words/dwords/qwords)
    dt <mod>!<type> [field] [addr]   PDB type size, or field offset (+value at addr)
    !vad [pid]             virtual address descriptors of a process
    !handle [pid]          handles of a process
    s <addr> <len> <hex>   search a byte pattern in a range
    info                   target/config summary
    help                   this help
    q | quit | exit        leave

Control channel (LIVE Hyper-V VM only, via hvlib - needs admin; wineye --vm <N>
or '.ctl <N>'). This lifts the passive-only limit for registers and run state:
    .ctl <N>               bind the control channel to VM index N
    freeze                 suspend the VM (SdkControlVmState)
    resume | g             resume the VM
    r [reg...]             read VP registers (rip/rsp/rax/.../rflags)
    NB: no instruction breakpoints / single-step here (hvlib delivers no trap
        events). For those, use WinDbg/kd via 'hvkdfusion.py debug'.

Addresses accept 0x.., decimal, and `module!symbol`. Scripting: `-c "cmd;cmd"`,
`--batch`, `--json` (structured commands), or import wineye and use the Vmm class.

GPL-AGPL (uses MemProcFS vmm.dll).
"""

import argparse
import ctypes
import json
import os
import sys
from ctypes import (POINTER, Structure, c_bool, c_char, c_char_p, c_int,
                    c_uint16, c_uint32, c_uint64, c_void_p, byref, cast, sizeof)
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"

PID_PHYSICAL = 0xFFFFFFFF            # VMMDLL: dwPID -1 reads physical memory
VMMDLL_PROCESS_INFORMATION_MAGIC = 0xc0ffee663df9301e
VMMDLL_PROCESS_INFORMATION_VERSION = 7
VMMDLL_MAP_MODULE_VERSION = 6
VMMDLL_MAP_VAD_VERSION = 6
VMMDLL_MAP_HANDLE_VERSION = 3

_INTEGRITY = {0: "-", 1: "untrusted", 2: "low", 3: "medium", 4: "medium+",
              5: "high", 6: "system", 7: "protected"}
_SYSTEM = {0: "unknown", 2: "windows-x64", 4: "windows-x86"}

# Hyper-V TLFS register codes (hvgdk.h) for the hvlib control channel.
HV_REG = {
    "rax": 0x00020000, "rcx": 0x00020001, "rdx": 0x00020002, "rbx": 0x00020003,
    "rsp": 0x00020004, "rbp": 0x00020005, "rsi": 0x00020006, "rdi": 0x00020007,
    "r8": 0x00020008, "r9": 0x00020009, "r10": 0x0002000A, "r11": 0x0002000B,
    "r12": 0x0002000C, "r13": 0x0002000D, "r14": 0x0002000E, "r15": 0x0002000F,
    "rip": 0x00020010, "rflags": 0x00020011,
}
_REG_ORDER = ["rip", "rsp", "rbp", "rflags", "rax", "rbx", "rcx", "rdx",
              "rsi", "rdi", "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"]


# --------------------------------------------------------------------------- #
#  ctypes structs (x64, natural MSVC alignment)
# --------------------------------------------------------------------------- #
class _PROCWIN(Structure):
    _fields_ = [
        ("vaEPROCESS", c_uint64), ("vaPEB", c_uint64), ("_Reserved1", c_uint64),
        ("fWow64", c_int), ("vaPEB32", c_uint32), ("dwSessionId", c_uint32),
        ("qwLUID", c_uint64), ("szSID", c_char * 260), ("IntegrityLevel", c_int),
    ]


class VMMDLL_PROCESS_INFORMATION(Structure):
    _fields_ = [
        ("magic", c_uint64), ("wVersion", c_uint16), ("wSize", c_uint16),
        ("tpMemoryModel", c_int), ("tpSystem", c_int), ("fUserOnly", c_int),
        ("dwPID", c_uint32), ("dwPPID", c_uint32), ("dwState", c_uint32),
        ("szName", c_char * 16), ("szNameLong", c_char * 64),
        ("paDTB", c_uint64), ("paDTB_UserOpt", c_uint64), ("win", _PROCWIN),
    ]


class VMMDLL_MAP_MODULEENTRY(Structure):
    _fields_ = [
        ("vaBase", c_uint64), ("vaEntry", c_uint64), ("cbImageSize", c_uint32),
        ("fWoW64", c_int), ("uszText", c_char_p),
        ("_Reserved3", c_uint32), ("_Reserved4", c_uint32),
        ("uszFullName", c_char_p), ("tp", c_int),
        ("cbFileSizeRaw", c_uint32), ("cSection", c_uint32),
        ("cEAT", c_uint32), ("cIAT", c_uint32), ("_Reserved2", c_uint32),
        ("_Reserved1", c_uint64 * 3),
        ("pExDebugInfo", c_void_p), ("pExVersionInfo", c_void_p),
    ]


class VMMDLL_MAP_MODULE(Structure):   # header only; entries follow as flex array
    _fields_ = [
        ("dwVersion", c_uint32), ("_Reserved1", c_uint32 * 5),
        ("pbMultiText", c_void_p), ("cbMultiText", c_uint32), ("cMap", c_uint32),
    ]


class VMMDLL_MAP_VADENTRY(Structure):
    _fields_ = [
        ("vaStart", c_uint64), ("vaEnd", c_uint64), ("vaVad", c_uint64),
        ("dw0", c_uint32), ("dw1", c_uint32),   # packed bitfields; decode by mask
        ("u2", c_uint32), ("cbPrototypePte", c_uint32),
        ("vaPrototypePte", c_uint64), ("vaSubsection", c_uint64),
        ("uszText", c_char_p), ("_FutureUse1", c_uint32), ("_Reserved1", c_uint32),
        ("vaFileObject", c_uint64), ("cVadExPages", c_uint32),
        ("cVadExPagesBase", c_uint32), ("_Reserved2", c_uint64),
    ]


class VMMDLL_MAP_VAD(Structure):      # header; entries follow (cMap @ offset 36)
    _fields_ = [
        ("dwVersion", c_uint32), ("_Reserved1", c_uint32 * 4), ("cPage", c_uint32),
        ("pbMultiText", c_void_p), ("cbMultiText", c_uint32), ("cMap", c_uint32),
    ]


class VMMDLL_MAP_HANDLEENTRY(Structure):
    _fields_ = [
        ("vaObject", c_uint64), ("dwHandle", c_uint32), ("dwAccessType", c_uint32),
        ("qwHandleCount", c_uint64), ("qwPointerCount", c_uint64),
        ("vaObjectCreateInfo", c_uint64), ("vaSecurityDescriptor", c_uint64),
        ("uszText", c_char_p), ("_FutureUse2", c_uint32), ("dwPID", c_uint32),
        ("dwPoolTag", c_uint32), ("_FutureUse", c_uint32 * 7), ("uszType", c_char_p),
    ]


class VMMDLL_MAP_HANDLE(Structure):   # header; entries follow (cMap @ offset 36)
    _fields_ = [
        ("dwVersion", c_uint32), ("_Reserved1", c_uint32 * 5),
        ("pbMultiText", c_void_p), ("cbMultiText", c_uint32), ("cMap", c_uint32),
    ]


# --------------------------------------------------------------------------- #
#  vmm.dll wrapper
# --------------------------------------------------------------------------- #
class VmmError(Exception):
    pass


class Vmm:
    def __init__(self, vmm_dll: str, argv: list[str]):
        d = Path(vmm_dll).parent
        try:
            os.add_dll_directory(str(d))      # so leechcore.dll etc. resolve
        except Exception:
            pass
        self._lib = ctypes.CDLL(vmm_dll)
        self._bind()
        argv = ["wineye"] + argv              # argv[0] is the program name
        arr = (c_char_p * len(argv))(*[a.encode() for a in argv])
        self.h = self._lib.VMMDLL_Initialize(len(argv), arr)
        if not self.h:
            raise VmmError("VMMDLL_Initialize a échoué (device/dump invalide, "
                           "ou admin requis pour un device live).")

    def _bind(self):
        L = self._lib
        L.VMMDLL_Initialize.restype = c_void_p
        L.VMMDLL_Initialize.argtypes = [c_uint32, POINTER(c_char_p)]
        L.VMMDLL_Close.argtypes = [c_void_p]
        L.VMMDLL_MemFree.argtypes = [c_void_p]
        L.VMMDLL_MemReadEx.restype = c_int
        L.VMMDLL_MemReadEx.argtypes = [c_void_p, c_uint32, c_uint64,
                                       c_void_p, c_uint32, POINTER(c_uint32), c_uint64]
        L.VMMDLL_PidList.restype = c_int
        L.VMMDLL_PidList.argtypes = [c_void_p, POINTER(c_uint32), POINTER(ctypes.c_size_t)]
        L.VMMDLL_ProcessGetInformation.restype = c_int
        L.VMMDLL_ProcessGetInformation.argtypes = [c_void_p, c_uint32,
                                                   POINTER(VMMDLL_PROCESS_INFORMATION),
                                                   POINTER(ctypes.c_size_t)]
        L.VMMDLL_Map_GetModuleU.restype = c_int
        L.VMMDLL_Map_GetModuleU.argtypes = [c_void_p, c_uint32,
                                            POINTER(POINTER(VMMDLL_MAP_MODULE)), c_uint32]
        L.VMMDLL_PdbSymbolAddress.restype = c_int
        L.VMMDLL_PdbSymbolAddress.argtypes = [c_void_p, c_char_p, c_char_p, POINTER(c_uint64)]
        L.VMMDLL_Map_GetVadU.restype = c_int
        L.VMMDLL_Map_GetVadU.argtypes = [c_void_p, c_uint32, c_int,
                                         POINTER(POINTER(VMMDLL_MAP_VAD))]
        L.VMMDLL_Map_GetHandleU.restype = c_int
        L.VMMDLL_Map_GetHandleU.argtypes = [c_void_p, c_uint32,
                                            POINTER(POINTER(VMMDLL_MAP_HANDLE))]
        L.VMMDLL_PdbTypeSize.restype = c_int
        L.VMMDLL_PdbTypeSize.argtypes = [c_void_p, c_char_p, c_char_p, POINTER(c_uint32)]
        L.VMMDLL_PdbTypeChildOffset.restype = c_int
        L.VMMDLL_PdbTypeChildOffset.argtypes = [c_void_p, c_char_p, c_char_p, c_char_p,
                                                POINTER(c_uint32)]

    # -- memory -- #
    def read(self, pid: int, addr: int, size: int) -> bytes:
        buf = (ctypes.c_ubyte * size)()
        got = c_uint32(0)
        ok = self._lib.VMMDLL_MemReadEx(self.h, c_uint32(pid & 0xFFFFFFFF),
                                        c_uint64(addr), buf, size, byref(got), 0)
        if not ok:
            raise VmmError(f"lecture impossible @ {addr:#x} (pid {pid}).")
        return bytes(buf[:got.value])

    # -- processes -- #
    def pids(self) -> list[int]:
        n = ctypes.c_size_t(0)
        self._lib.VMMDLL_PidList(self.h, None, byref(n))
        arr = (c_uint32 * n.value)()
        if not self._lib.VMMDLL_PidList(self.h, arr, byref(n)):
            raise VmmError("VMMDLL_PidList a échoué.")
        return list(arr[:n.value])

    def process(self, pid: int) -> dict | None:
        info = VMMDLL_PROCESS_INFORMATION()
        info.magic = VMMDLL_PROCESS_INFORMATION_MAGIC
        info.wVersion = VMMDLL_PROCESS_INFORMATION_VERSION
        info.wSize = sizeof(VMMDLL_PROCESS_INFORMATION)
        cb = ctypes.c_size_t(sizeof(VMMDLL_PROCESS_INFORMATION))
        if not self._lib.VMMDLL_ProcessGetInformation(self.h, c_uint32(pid), byref(info), byref(cb)):
            return None
        return {
            "pid": info.dwPID, "ppid": info.dwPPID,
            "name": info.szName.decode(errors="replace"),
            "name_long": info.szNameLong.decode(errors="replace"),
            "eprocess": info.win.vaEPROCESS, "peb": info.win.vaPEB,
            "dtb": info.paDTB, "wow64": bool(info.win.fWow64),
            "session": info.win.dwSessionId,
            "integrity": _INTEGRITY.get(info.win.IntegrityLevel, "?"),
            "sid": info.win.szSID.decode(errors="replace"),
            "system": _SYSTEM.get(info.tpSystem, "?"),
        }

    def modules(self, pid: int) -> list[dict]:
        pp = POINTER(VMMDLL_MAP_MODULE)()
        if not self._lib.VMMDLL_Map_GetModuleU(self.h, c_uint32(pid), byref(pp), 0):
            raise VmmError(f"Map_GetModule a échoué (pid {pid}).")
        try:
            hdr = pp.contents
            if hdr.dwVersion != VMMDLL_MAP_MODULE_VERSION:
                raise VmmError(f"version de MAP_MODULE inattendue: {hdr.dwVersion}")
            base = ctypes.addressof(hdr) + sizeof(VMMDLL_MAP_MODULE)
            entries = cast(base, POINTER(VMMDLL_MAP_MODULEENTRY))
            out = []
            for i in range(hdr.cMap):
                e = entries[i]
                out.append({
                    "base": e.vaBase, "size": e.cbImageSize,
                    "entry": e.vaEntry, "wow64": bool(e.fWoW64),
                    "name": (e.uszText or b"").decode(errors="replace"),
                    "path": (e.uszFullName or b"").decode(errors="replace"),
                })
            return out
        finally:
            self._lib.VMMDLL_MemFree(cast(pp, c_void_p))

    def symbol(self, module: str, name: str) -> int:
        va = c_uint64(0)
        if not self._lib.VMMDLL_PdbSymbolAddress(self.h, module.encode(), name.encode(), byref(va)):
            raise VmmError(f"symbole introuvable: {module}!{name} "
                           "(symboles chargés ? module 'nt' d'abord disponible)")
        return va.value

    def vads(self, pid: int) -> list[dict]:
        pp = POINTER(VMMDLL_MAP_VAD)()
        if not self._lib.VMMDLL_Map_GetVadU(self.h, c_uint32(pid), 1, byref(pp)):
            raise VmmError(f"Map_GetVad a échoué (pid {pid}).")
        try:
            hdr = pp.contents
            base = ctypes.addressof(hdr) + sizeof(VMMDLL_MAP_VAD)
            entries = cast(base, POINTER(VMMDLL_MAP_VADENTRY))
            out = []
            for i in range(hdr.cMap):
                e = entries[i]
                out.append({
                    "start": e.vaStart, "end": e.vaEnd, "vad": e.vaVad,
                    "protection": (e.dw0 >> 3) & 0x1F,
                    "image": bool((e.dw0 >> 8) & 1), "file": bool((e.dw0 >> 9) & 1),
                    "stack": bool((e.dw0 >> 13) & 1),
                    "text": (e.uszText or b"").decode(errors="replace"),
                })
            return out
        finally:
            self._lib.VMMDLL_MemFree(cast(pp, c_void_p))

    def handles(self, pid: int) -> list[dict]:
        pp = POINTER(VMMDLL_MAP_HANDLE)()
        if not self._lib.VMMDLL_Map_GetHandleU(self.h, c_uint32(pid), byref(pp)):
            raise VmmError(f"Map_GetHandle a échoué (pid {pid}).")
        try:
            hdr = pp.contents
            base = ctypes.addressof(hdr) + sizeof(VMMDLL_MAP_HANDLE)
            entries = cast(base, POINTER(VMMDLL_MAP_HANDLEENTRY))
            out = []
            for i in range(hdr.cMap):
                e = entries[i]
                out.append({
                    "handle": e.dwHandle, "object": e.vaObject,
                    "access": e.dwAccessType & 0xFFFFFF,
                    "type": (e.uszType or b"").decode(errors="replace"),
                    "text": (e.uszText or b"").decode(errors="replace"),
                })
            return out
        finally:
            self._lib.VMMDLL_MemFree(cast(pp, c_void_p))

    def type_size(self, module: str, typ: str) -> int:
        cb = c_uint32(0)
        if not self._lib.VMMDLL_PdbTypeSize(self.h, module.encode(), typ.encode(), byref(cb)):
            raise VmmError(f"type introuvable: {module}!{typ}")
        return cb.value

    def type_child_offset(self, module: str, typ: str, child: str) -> int:
        off = c_uint32(0)
        if not self._lib.VMMDLL_PdbTypeChildOffset(self.h, module.encode(),
                                                   typ.encode(), child.encode(), byref(off)):
            raise VmmError(f"champ introuvable: {module}!{typ}.{child}")
        return off.value

    def close(self):
        if getattr(self, "h", None):
            self._lib.VMMDLL_Close(self.h)
            self.h = None


# --------------------------------------------------------------------------- #
#  Control channel via hvlib (lifts the passive-only limit on LIVE Hyper-V VMs)
# --------------------------------------------------------------------------- #
class HvControl:
    """Freeze/resume + VP registers for a live Hyper-V VM, through hvlib.

    Passive memory cannot stop the CPU or read registers; hvlib (LiveCloudKd)
    provides that control channel (SdkControlVmState / SdkReadVpRegister). This
    binds an hvlib session to VM index `vm_index` (needs admin + Hyper-V). It
    does NOT give instruction breakpoints/single-step - hvlib delivers no trap
    events; for those use WinDbg/kd (hvkdfusion.py debug).
    """
    def __init__(self, vm_index: int):
        import hvkdfusion
        self.cfg = hvkdfusion.load_config()
        self.mod, self.engine, vms = hvkdfusion.engine_enum(self.cfg)
        if vm_index >= len(vms):
            self.engine.cleanup()
            raise VmmError(f"VM index {vm_index} hors plage (sur {len(vms)}).")
        self.vm_index = vm_index
        self.handle = self.engine.SelectPartition(vm_index)
        lib = self.engine.hvlib
        lib.SdkReadVpRegister.restype = c_bool
        lib.SdkReadVpRegister.argtypes = [c_uint64, c_uint32, c_int, c_uint32, c_void_p]
        self.lib = lib

    def reg(self, code: int, vp: int = 0, vtl: int = 0) -> int:
        val = (ctypes.c_ubyte * 16)()
        if not self.lib.SdkReadVpRegister(c_uint64(self.handle), c_uint32(vp),
                                          c_int(vtl), c_uint32(code), val):
            raise VmmError("SdkReadVpRegister a échoué.")
        return int.from_bytes(bytes(val[:8]), "little")

    def _state(self, action_name: str) -> bool:
        action = getattr(self.mod.VmStateAction, action_name)
        method = self.mod.SuspendResumeMethod.SuspendResumePowershell
        return bool(self.lib.SdkControlVmState(c_uint64(self.handle),
                    c_uint32(int(action)), c_uint32(int(method)), c_bool(True)))

    def freeze(self) -> bool:
        return self._state("SuspendVm")

    def resume(self) -> bool:
        return self._state("ResumeVm")

    def close(self):
        try:
            self.engine.cleanup()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  REPL
# --------------------------------------------------------------------------- #
class Repl:
    def __init__(self, vmm: Vmm, json_out: bool = False, ctl: "HvControl | None" = None):
        self.vmm = vmm
        self.pid = 4          # default context: System (kernel VA); -1 = physical
        self.json = json_out
        self.ctl = ctl        # optional live-VM control channel (hvlib)

    def _emit(self, text: str, data=None):
        if self.json and data is not None:
            print(json.dumps(data, default=lambda o: hex(o) if isinstance(o, int) else str(o)))
        else:
            print(text)

    def resolve(self, token: str) -> int:
        token = token.strip()
        if "!" in token:
            mod, sym = token.split("!", 1)
            return self.vmm.symbol(mod, sym)
        return int(token, 0)

    def run(self, line: str) -> bool:
        line = line.strip()
        if not line or line.startswith(("*", ";")):
            return True
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        try:
            if cmd in ("q", "quit", "exit", ".quit"):
                return False
            elif cmd in ("help", "?", ".help"):
                print(__doc__.split("Commands", 1)[1] if "Commands" in __doc__ else __doc__)
            elif cmd == "info":
                self._cmd_info()
            elif cmd == "ps":
                self._cmd_ps()
            elif cmd in ("!process", "proc"):
                self._cmd_process(args)
            elif cmd == ".pid":
                if args:
                    self.pid = int(args[0], 0)
                print(f"contexte pid = {self.pid if self.pid != PID_PHYSICAL and self.pid != -1 else 'physique'}")
            elif cmd == "lm":
                self._cmd_lm(args)
            elif cmd == "x":
                self._cmd_x(args)
            elif cmd in ("db", "dw", "dd", "dq"):
                self._cmd_dump(cmd, args)
            elif cmd == "s":
                self._cmd_search(args)
            elif cmd == "dt":
                self._cmd_dt(args)
            elif cmd in ("!handle", "handles"):
                self._cmd_handles(args)
            elif cmd in ("!vad", "vad"):
                self._cmd_vad(args)
            elif cmd == ".ctl":
                self._cmd_ctl(args)
            elif cmd in ("freeze", ".freeze"):
                self._cmd_freeze_resume("freeze")
            elif cmd in ("resume", ".resume", "g"):
                self._cmd_freeze_resume("resume")
            elif cmd == "r":
                self._cmd_registers(args)
            else:
                print(f"commande inconnue: {cmd}  (help)")
        except VmmError as e:
            print(f"[!] {e}")
        except ValueError as e:
            print(f"[!] argument invalide: {e}")
        return True

    def _pid_arg(self, args) -> int:
        return int(args[0], 0) if args else self.pid

    def _cmd_info(self):
        pids = self.vmm.pids()
        sysproc = self.vmm.process(4) or {}
        self._emit(
            f"processus: {len(pids)}   système: {sysproc.get('system', '?')}   "
            f"contexte pid: {self.pid}",
            {"process_count": len(pids), "system": sysproc.get("system"), "ctx_pid": self.pid})

    def _cmd_ps(self):
        rows = []
        for pid in sorted(self.vmm.pids()):
            p = self.vmm.process(pid)
            if p:
                rows.append(p)
        if self.json:
            print(json.dumps(rows, default=lambda o: hex(o) if isinstance(o, int) else o))
            return
        print(f"{'PID':>6} {'PPID':>6}  {'EPROCESS':>18}  {'PEB':>18}  Name")
        print("-" * 70)
        for p in rows:
            print(f"{p['pid']:>6} {p['ppid']:>6}  {p['eprocess']:#018x}  "
                  f"{p['peb']:#018x}  {p['name']}{'  (Wow64)' if p['wow64'] else ''}")

    def _cmd_process(self, args):
        pid = self._pid_arg(args)
        p = self.vmm.process(pid)
        if not p:
            print(f"[!] pid {pid} introuvable")
            return
        if self.json:
            print(json.dumps(p, default=lambda o: hex(o) if isinstance(o, int) else o))
            return
        print(f"  PID {p['pid']}  PPID {p['ppid']}  '{p['name_long'] or p['name']}'")
        print(f"  EPROCESS {p['eprocess']:#x}   PEB {p['peb']:#x}   DTB {p['dtb']:#x}")
        print(f"  session {p['session']}  integrité {p['integrity']}  wow64 {p['wow64']}")
        print(f"  SID {p['sid']}")

    def _cmd_lm(self, args):
        pid = self._pid_arg(args)
        mods = self.vmm.modules(pid)
        if self.json:
            print(json.dumps(mods, default=lambda o: hex(o) if isinstance(o, int) else o))
            return
        print(f"{'base':>18}  {'size':>9}  module   (pid {pid})")
        print("-" * 60)
        for m in mods:
            print(f"{m['base']:#018x}  {m['size']:>9}  {m['name']}")

    def _cmd_x(self, args):
        if not args or "!" not in args[0]:
            print("usage: x <module>!<symbole>   (ex. x nt!PsInitialSystemProcess)")
            return
        mod, sym = args[0].split("!", 1)
        va = self.vmm.symbol(mod, sym)
        self._emit(f"{va:#018x}  {mod}!{sym}", {"module": mod, "symbol": sym, "address": va})

    def _cmd_dump(self, cmd, args):
        if not args:
            print(f"usage: {cmd} <addr|module!sym> [L<count>]")
            return
        addr = self.resolve(args[0])
        count = 128
        for a in args[1:]:
            if a.lower().startswith("l"):
                count = int(a[1:], 0)
        width = {"db": 16, "dw": 16, "dd": 16, "dq": 16}[cmd]
        size = count if cmd == "db" else count * {"dw": 2, "dd": 4, "dq": 8}[cmd]
        data = self.vmm.read(self.pid, addr, size)
        if self.json:
            print(json.dumps({"address": hex(addr), "pid": self.pid, "hex": data.hex()}))
            return
        self._hexdump(cmd, addr, data)

    def _hexdump(self, cmd, addr, data):
        if cmd == "db":
            for off in range(0, len(data), 16):
                chunk = data[off:off + 16]
                hexs = " ".join(f"{b:02x}" for b in chunk).ljust(47)
                asci = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                print(f"{addr + off:#018x}  {hexs}  {asci}")
        else:
            step = {"dw": 2, "dd": 4, "dq": 8}[cmd]
            per = {"dw": 8, "dd": 4, "dq": 2}[cmd]
            vals = [int.from_bytes(data[i:i + step], "little")
                    for i in range(0, len(data) - step + 1, step)]
            for i in range(0, len(vals), per):
                row = vals[i:i + per]
                w = step * 2
                print(f"{addr + i * step:#018x}  " + " ".join(f"{v:0{w}x}" for v in row))

    def _cmd_search(self, args):
        if len(args) < 3:
            print("usage: s <addr|sym> <len> <hexpattern>   (ex. s nt!x 0x10000 48894c)")
            return
        start = self.resolve(args[0])
        length = int(args[1], 0)
        pat = bytes.fromhex(args[2].replace(" ", ""))
        data = self.vmm.read(self.pid, start, length)
        hits = []
        i = data.find(pat)
        while i != -1 and len(hits) < 256:
            hits.append(start + i)
            i = data.find(pat, i + 1)
        if self.json:
            print(json.dumps({"pattern": pat.hex(), "hits": [hex(h) for h in hits]}))
            return
        print(f"{len(hits)} occurrence(s):")
        for h in hits:
            print(f"  {h:#018x}")

    def _cmd_dt(self, args):
        """dt <module>!<type> [champ] [addr]  -> taille, ou offset/valeur d'un champ."""
        if not args or "!" not in args[0]:
            print("usage: dt <module>!<type> [champ] [addr|sym]")
            return
        mod, typ = args[0].split("!", 1)
        if len(args) == 1:
            size = self.vmm.type_size(mod, typ)
            self._emit(f"{mod}!{typ}  taille {size:#x} ({size})",
                       {"type": f"{mod}!{typ}", "size": size})
            return
        child = args[1]
        off = self.vmm.type_child_offset(mod, typ, child)
        line = f"+{off:#06x}  {typ}.{child}"
        data = {"type": f"{mod}!{typ}", "child": child, "offset": off}
        if len(args) >= 3:   # read the field value at a base address
            base = self.resolve(args[2])
            raw = self.vmm.read(self.pid, base + off, 8)
            val = int.from_bytes(raw, "little")
            line += f"  @ {base + off:#x} = {val:#x}"
            data["address"] = base + off
            data["value"] = val
        self._emit(line, data)

    def _cmd_handles(self, args):
        pid = self._pid_arg(args)
        hs = self.vmm.handles(pid)
        if self.json:
            print(json.dumps(hs, default=lambda o: hex(o) if isinstance(o, int) else o))
            return
        print(f"{'handle':>8}  {'object':>18}  {'type':<16} detail   (pid {pid})")
        print("-" * 70)
        for x in hs:
            print(f"{x['handle']:#08x}  {x['object']:#018x}  {x['type']:<16} {x['text']}")

    def _cmd_vad(self, args):
        pid = self._pid_arg(args)
        vs = self.vmm.vads(pid)
        if self.json:
            print(json.dumps(vs, default=lambda o: hex(o) if isinstance(o, int) else o))
            return
        print(f"{'start':>18} {'end':>18}  flags  detail   (pid {pid})")
        print("-" * 72)
        for v in vs:
            fl = ("I" if v["image"] else "") + ("F" if v["file"] else "") + ("S" if v["stack"] else "")
            print(f"{v['start']:#018x} {v['end']:#018x}  {fl:<5}  {v['text']}")

    # -- control channel (live Hyper-V VM, via hvlib) -- #
    def _need_ctl(self) -> bool:
        if self.ctl is None:
            print("[!] pas de canal de contrôle. Lance wineye --live --vm <N>, ou "
                  "'.ctl <N>' ici (VM Hyper-V + admin requis). Pour breakpoints / "
                  "pas-à-pas : WinDbg via 'hvkdfusion.py debug'.")
            return False
        return True

    def _cmd_ctl(self, args):
        if not args:
            print("usage: .ctl <index_VM>   (ouvre le canal de contrôle hvlib)")
            return
        try:
            self.ctl = HvControl(int(args[0], 0))
            print(f"[+] canal de contrôle lié à la VM {args[0]}")
        except Exception as e:
            print(f"[!] {e}")

    def _cmd_freeze_resume(self, which):
        if not self._need_ctl():
            return
        ok = self.ctl.freeze() if which == "freeze" else self.ctl.resume()
        print(f"[{'+' if ok else '!'}] {which}: {bool(ok)}")

    def _cmd_registers(self, args):
        if not self._need_ctl():
            return
        names = [a.lower() for a in args] if args else _REG_ORDER
        vals = {}
        for n in names:
            if n not in HV_REG:
                print(f"  registre inconnu: {n}")
                continue
            try:
                vals[n] = self.ctl.reg(HV_REG[n])
            except VmmError as e:
                print(f"  {n}: {e}")
        if self.json:
            print(json.dumps({k: hex(v) for k, v in vals.items()}))
            return
        items = list(vals.items())
        for i in range(0, len(items), 3):
            print("  " + "   ".join(f"{k:>6}={v:#018x}" for k, v in items[i:i + 3]))


# --------------------------------------------------------------------------- #
#  target resolution + CLI
# --------------------------------------------------------------------------- #
def find_vmm_dll(explicit: str | None) -> str:
    if explicit and Path(explicit).exists():
        return explicit
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        mp = cfg.get("memprocfs_exe") or ""
        if mp:
            cand = Path(mp).parent / "vmm.dll"
            if cand.exists():
                return str(cand)
        lc = cfg.get("leechcore_dll") or ""
        if lc and (Path(lc).parent / "vmm.dll").exists():
            return str(Path(lc).parent / "vmm.dll")
    raise SystemExit("[!] vmm.dll introuvable. Lance d'abord 'hvkdfusion.py setup "
                     "--memprocfs-exe <...>' ou passe --vmm <chemin vmm.dll>.")


def build_device_args(args) -> list[str]:
    if args.dump:
        return ["-device", args.dump]
    if args.device:
        return ["-device", args.device]
    if args.live:
        return ["-device", "hvmm"]
    if args.from_vm is not None:
        # produce a linear dump via the engine (reuses hvkdfusion), then mount it
        import hvkdfusion
        cfg = hvkdfusion.load_config()
        out = args.out or str(HERE / f"vm{args.from_vm}.raw")
        hvkdfusion.engine_linear_dump(cfg, args.from_vm, out)
        return ["-device", out]
    raise SystemExit("[!] cible requise : --dump <f> | --device <x> | --live | --from-vm <N>")


def main(argv=None):
    p = argparse.ArgumentParser(prog="wineye",
        description="REPL natif Windows façon WinDbg sur mémoire passive (vmm.dll).")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dump", help="dump à analyser (.raw/.dmp)")
    g.add_argument("--device", help="device LeechCore brut (hvmm, fpga, pmem, ...)")
    g.add_argument("--live", action="store_true", help="VM Hyper-V live (device hvmm)")
    g.add_argument("--from-vm", type=int, help="dump linéaire de la VM N puis analyse")
    p.add_argument("--out", help="chemin du .raw (avec --from-vm)")
    p.add_argument("--vm", type=int, help="index VM pour le canal de contrôle hvlib (freeze/resume/r)")
    p.add_argument("--vmm", help="chemin de vmm.dll (sinon via config.json)")
    p.add_argument("-c", "--command", action="append", help="commande(s) à exécuter (répétable)")
    p.add_argument("--batch", action="store_true", help="quitter après -c")
    p.add_argument("--json", action="store_true", help="sortie JSON (commandes structurées)")
    args = p.parse_args(argv)

    vmm_dll = find_vmm_dll(args.vmm)
    device = build_device_args(args)
    print(f"[*] vmm.dll: {vmm_dll}")
    print(f"[*] cible  : {' '.join(device)}")
    try:
        vmm = Vmm(vmm_dll, device + ["-waitinitialize"])
    except VmmError as e:
        raise SystemExit(f"[!] {e}")
    ctl = None
    if args.vm is not None:
        try:
            ctl = HvControl(args.vm)
            print(f"[*] contrôle: canal hvlib lié à la VM {args.vm} (freeze/resume/r)")
        except Exception as e:
            print(f"[~] canal de contrôle indisponible: {e}")
    repl = Repl(vmm, json_out=args.json, ctl=ctl)
    try:
        for c in (args.command or []):
            for one in c.split(";"):
                if one.strip():
                    print(f"wineye> {one.strip()}")
                    repl.run(one)
        if args.batch and args.command:
            return
        print("wineye — tape 'help' pour les commandes, 'q' pour quitter.")
        while True:
            try:
                line = input("wineye> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not repl.run(line):
                break
    finally:
        vmm.close()


if __name__ == "__main__":
    main()
