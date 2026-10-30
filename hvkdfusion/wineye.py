#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wineye - a native-Windows, passive, WinDbg-style memory inspector.

This is the Windows-native counterpart to ntoseye's *memory backend*: it
inspects a Windows target from memory alone (a crash/raw dump, or a live
Hyper-V VM), with no debug transport in the guest and the target unaware it is
being looked at. It drives MemProcFS's vmm.dll directly through ctypes, or reads
kernel crash dumps itself; it has its own PDB reader, x64 stack unwinder and
x86-64 disassembler - no WSL, no pip packages, no Python extension.

What it is NOT: it is not a full re-implementation of ntoseye. Passive memory
gives no run control (no stepping, no breakpoints, no register writes) - the
same limits ntoseye states for its `memory` backend. For run control on
Windows, use WinDbg/kd via `hvkdfusion.py debug`.

Commands (WinDbg-flavoured):
    ps                     list processes
    !process [pid] [flags] process details; flags 2|4: its threads, 7: threads + stacks
    .pid <n|-1>            set process context (-1 = physical memory)
    lm [pid]               list modules of a process (default: current)
    x <module>!<symbol>    resolve a symbol via PDB (e.g. x nt!PsInitialSystemProcess)
    db|dw|dd|dq <addr> [L<n>]   hex dump (bytes/words/dwords/qwords)
    dt <mod>!<type> [field] [addr]   PDB type size, or field offset (+value at addr)
    !vad [pid]             virtual address descriptors of a process
    !handle [pid]          handles of a process (vmm.dll engine)
    s <addr> <len> <hex>   search a byte pattern in a range
    info                   target/config summary
    help                   this help
    q | quit | exit        leave

Debugger (crash dumps, images, live VMs; symbols from the PDBs):
    !analyze [-v]          bugcheck analysis: stop code and arguments, faulting
                           context (trap frame / context record), instruction,
                           module, symbol and stack
    k [n]                  call stack of the current context, kernel then user mode
                           across the system call; source lines with a private PDB
    kb [n]                 same, with the 4 parameter home slots ("Args to Child")
    ~                      processors: current thread, process, rip
                           (user-mode minidump: its threads)
    ~<n>                   switch to processor n (its saved context) / thread n
    !thread [ethread]      thread details and stack (default: current thread)
    .thread <ethread>      switch the context to a thread (switch frame / processor)
    .cxr [addr]            context from a CONTEXT record (no argument: back to default)
    .trap <addr>           context from a KTRAP_FRAME
    r [reg...]             registers of the current context (frame selected with .frame)
    u [addr] [L<n>]        disassemble (default: rip, 8 instructions; repeat to continue)
    uf <addr|mod!func>     disassemble a whole function (flow analysis, split chunks)
    ln <addr>              nearest symbol and source line
    .frame [n]             select a frame of the last k (n in hex, as k prints it)
    dv                     locals and parameters of the selected frame (private PDB;
                           without one: the parameter home slots, marked unreliable)
    .dump /u [pid] <file>  write a full user-mode minidump of a process: threads with
                           their user-mode registers, modules, every resident page
                           (from a kernel dump: threads + modules, no memory)
    .sympath [p1;p2...]    private PDB search path (also --sympath / WINEYE_SYMPATH)
    .reload                reload symbols

Control channel (LIVE Hyper-V VM only, via hvlib - needs admin; wineye --vm <N>
or '.ctl <N>'). This lifts the passive-only limit for registers and run state:
    .ctl <N>               bind the control channel to VM index N
    freeze                 suspend the VM (SdkControlVmState)
    resume | g             resume the VM
    r [reg...]             read VP registers (rip/rsp/rax/.../rflags)
    NB: no instruction breakpoints / single-step here (hvlib delivers no trap
        events). For those, use WinDbg/kd via 'hvkdfusion.py debug'.

Addresses are expressions: hexadecimal by default as in WinDbg (fffff807`50001000,
0x.. too), 0n for decimal, registers of the current context (rsp, @rip),
module!symbol, combined with + and -. Counts (L<n>, lengths) are decimal or 0x...
Scripting: `-c "cmd;cmd"`, `--batch`, `--json` (structured commands), or import
wineye and use the Vmm / NativeEngine / KernelDebugger classes.

Engines (--engine): vmm.dll (MemProcFS: dumps, raw images, LeechCore devices,
live Hyper-V VMs) or native (Windows x64 kernel crash dumps .dmp, all formats:
full, bitmap, kernel, kernel+user, complete, live - no MemProcFS needed). auto
uses vmm.dll and falls back to native for a .dmp. User-mode minidumps (MDMP:
WinDbg .dump, MiniDumpWriteDump, Task Manager, .dump /u) open with their own
engine: ~ lists the threads, !analyze reads the exception record. Symbols: wineye
reads PDBs itself and downloads Microsoft's (PDB, or the image when a module
header is not in memory) into ~/.wineye/symbols; --sympath for private PDBs (and
local copies of images), --no-symbol-download to stay offline.

Kernel dumps hold no user-mode pages: wineye then rebuilds each process' modules
from its VADs (image section: name, SizeOfImage, CheckSum, file size and, from
Windows 10 2004, TimeDateStamp), so the user-mode frame where each thread entered
the kernel is still symbolized; the rest of the user stack is not in the dump.

Server modes (the ones ntoseye has; here native, no WSL) - `--serve`:
    --serve mcp              MCP server over stdio (JSON-RPC 2.0): tools
                             list_processes / process_info / list_modules /
                             list_handles / list_vads / read_memory /
                             resolve_symbol / registers / stack / analyze /
                             disassemble, for an AI/agent client.
    --serve gdb [--listen H:P]   GDB Remote Serial Protocol over TCP
                             (default 127.0.0.1:2159): 'target remote' then read
                             memory (x/...) and, on a live VM, registers.
    --serve dap [--listen H:P]   Debug Adapter Protocol (stdio, or TCP with
                             --listen): modules, stackTrace, readMemory, evaluate.
All server modes are read-only passive memory (VP registers only on a live VM);
no breakpoints/stepping - for run control use WinDbg/kd (hvkdfusion.py debug).

GPL-AGPL (uses MemProcFS vmm.dll).
"""

import argparse
import bisect
import ctypes
import json
import mmap
import os
import struct
import sys
import time
import urllib.request
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
        # vmm.dll parses argv from index 0 and only skips empty strings: a
        # program name there is read as an unknown option and fails the init
        argv = [""] + argv
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
#  x86-64 disassembler (u / uf), pure Python, Intel syntax in WinDbg style
# --------------------------------------------------------------------------- #
_GPR64 = ["rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi",
          "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"]
_GPR32 = ["eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi",
          "r8d", "r9d", "r10d", "r11d", "r12d", "r13d", "r14d", "r15d"]
_GPR16 = ["ax", "cx", "dx", "bx", "sp", "bp", "si", "di",
          "r8w", "r9w", "r10w", "r11w", "r12w", "r13w", "r14w", "r15w"]
_GPR8_REX = ["al", "cl", "dl", "bl", "spl", "bpl", "sil", "dil",
             "r8b", "r9b", "r10b", "r11b", "r12b", "r13b", "r14b", "r15b"]
_GPR8_LEGACY = ["al", "cl", "dl", "bl", "ah", "ch", "dh", "bh"]
_SEG = ["es", "cs", "ss", "ds", "fs", "gs", "?", "?"]
_CC = ["o", "no", "b", "ae", "e", "ne", "be", "a", "s", "ns", "p", "np", "l", "ge", "le", "g"]
_MEMSIZE = {1: "byte", 2: "word", 4: "dword", 6: "fword", 8: "qword", 10: "tbyte",
            16: "xmmword", 32: "ymmword", 64: "zmmword"}

# One-byte opcode map: mnemonic, operands. Operand codes (Intel manual style):
#   E/G modrm rm/reg, b/w/d/q/v/z/y sizes, I immediate, J relative, Z register in
#   the opcode, O moffs, M memory only, S segment register, X/Y string operands,
#   "AL"/"rAX"/"CL"/"DX"/"1" fixed. "d64": operand size defaults to 64 bits.
_OP1 = {}
for _i, _m in enumerate(["add", "or", "adc", "sbb", "and", "sub", "xor", "cmp"]):
    _b = _i * 8
    _OP1[_b + 0] = (_m, "Eb,Gb")
    _OP1[_b + 1] = (_m, "Ev,Gv")
    _OP1[_b + 2] = (_m, "Gb,Eb")
    _OP1[_b + 3] = (_m, "Gv,Ev")
    _OP1[_b + 4] = (_m, "AL,Ib")
    _OP1[_b + 5] = (_m, "rAX,Iz")
for _r in range(8):
    _OP1[0x50 + _r] = ("push", "Zv64")
    _OP1[0x58 + _r] = ("pop", "Zv64")
    _OP1[0xB0 + _r] = ("mov", "Zb,Ib")
    _OP1[0xB8 + _r] = ("mov", "Zv,Iv")
for _r in range(1, 8):
    _OP1[0x90 + _r] = ("xchg", "Zv,rAX")
for _c in range(16):
    _OP1[0x70 + _c] = ("j" + _CC[_c], "Jb")
_OP1.update({
    0x63: ("movsxd", "Gv,Ed"), 0x68: ("push", "Iz64"), 0x69: ("imul", "Gv,Ev,Iz"),
    0x6A: ("push", "Ib64"), 0x6B: ("imul", "Gv,Ev,Ibs"), 0x6C: ("insb", ""), 0x6D: ("ins", "sz"),
    0x6E: ("outsb", ""), 0x6F: ("outs", "sz"),
    0x84: ("test", "Eb,Gb"), 0x85: ("test", "Ev,Gv"), 0x86: ("xchg", "Eb,Gb"), 0x87: ("xchg", "Ev,Gv"),
    0x88: ("mov", "Eb,Gb"), 0x89: ("mov", "Ev,Gv"), 0x8A: ("mov", "Gb,Eb"), 0x8B: ("mov", "Gv,Ev"),
    0x8C: ("mov", "Ev,Sw"), 0x8D: ("lea", "Gv,M"), 0x8E: ("mov", "Sw,Ew"), 0x8F: ("pop", "Ev64"),
    0x90: ("nop", ""), 0x98: ("cbw", "cbw"), 0x99: ("cwd", "cwd"), 0x9B: ("fwait", ""),
    0x9C: ("pushf", "f64"), 0x9D: ("popf", "f64"), 0x9E: ("sahf", ""), 0x9F: ("lahf", ""),
    0xA0: ("mov", "AL,Ob"), 0xA1: ("mov", "rAX,Ov"), 0xA2: ("mov", "Ob,AL"), 0xA3: ("mov", "Ov,rAX"),
    0xA4: ("movs", "Xb"), 0xA5: ("movs", "Xv"), 0xA6: ("cmps", "Xb"), 0xA7: ("cmps", "Xv"),
    0xA8: ("test", "AL,Ib"), 0xA9: ("test", "rAX,Iz"), 0xAA: ("stos", "Xb"), 0xAB: ("stos", "Xv"),
    0xAC: ("lods", "Xb"), 0xAD: ("lods", "Xv"), 0xAE: ("scas", "Xb"), 0xAF: ("scas", "Xv"),
    0xC2: ("ret", "Iw"), 0xC3: ("ret", ""), 0xC8: ("enter", "Iw,Ib"), 0xC9: ("leave", ""),
    0xCA: ("retf", "Iw"), 0xCB: ("retf", ""), 0xCC: ("int", "3"), 0xCD: ("int", "Ib"),
    0xCF: ("iret", "iret"), 0xD7: ("xlat", ""),
    0xE0: ("loopne", "Jb"), 0xE1: ("loope", "Jb"), 0xE2: ("loop", "Jb"), 0xE3: ("jrcxz", "Jb"),
    0xE4: ("in", "AL,Ib"), 0xE5: ("in", "eAX,Ib"), 0xE6: ("out", "Ib,AL"), 0xE7: ("out", "Ib,eAX"),
    0xE8: ("call", "Jz"), 0xE9: ("jmp", "Jz"), 0xEB: ("jmp", "Jb"),
    0xEC: ("in", "AL,DX"), 0xED: ("in", "eAX,DX"), 0xEE: ("out", "DX,AL"), 0xEF: ("out", "DX,eAX"),
    0xF1: ("int1", ""), 0xF4: ("hlt", ""), 0xF5: ("cmc", ""), 0xF8: ("clc", ""), 0xF9: ("stc", ""),
    0xFA: ("cli", ""), 0xFB: ("sti", ""), 0xFC: ("cld", ""), 0xFD: ("std", ""),
})
# Opcode groups (modrm.reg selects the mnemonic)
_GRP = {
    0x80: (["add", "or", "adc", "sbb", "and", "sub", "xor", "cmp"], "Eb,Ib"),
    0x81: (["add", "or", "adc", "sbb", "and", "sub", "xor", "cmp"], "Ev,Iz"),
    0x83: (["add", "or", "adc", "sbb", "and", "sub", "xor", "cmp"], "Ev,Ibs"),
    0xC0: (["rol", "ror", "rcl", "rcr", "shl", "shr", "sal", "sar"], "Eb,Ib"),
    0xC1: (["rol", "ror", "rcl", "rcr", "shl", "shr", "sal", "sar"], "Ev,Ib"),
    0xD0: (["rol", "ror", "rcl", "rcr", "shl", "shr", "sal", "sar"], "Eb,1"),
    0xD1: (["rol", "ror", "rcl", "rcr", "shl", "shr", "sal", "sar"], "Ev,1"),
    0xD2: (["rol", "ror", "rcl", "rcr", "shl", "shr", "sal", "sar"], "Eb,CL"),
    0xD3: (["rol", "ror", "rcl", "rcr", "shl", "shr", "sal", "sar"], "Ev,CL"),
    0xF6: (["test", "test", "not", "neg", "mul", "imul", "div", "idiv"], "Eb"),
    0xF7: (["test", "test", "not", "neg", "mul", "imul", "div", "idiv"], "Ev"),
    0xFE: (["inc", "dec", None, None, None, None, None, None], "Eb"),
    0xFF: (["inc", "dec", "call", "call", "jmp", "jmp", "push", None], "Ev"),
    0xC6: (["mov", None, None, None, None, None, None, None], "Eb,Ib"),
    0xC7: (["mov", None, None, None, None, None, None, None], "Ev,Iz"),
}
_INVALID64 = {0x06, 0x07, 0x0E, 0x16, 0x17, 0x1E, 0x1F, 0x27, 0x2F, 0x37, 0x3F,
              0x60, 0x61, 0x82, 0x9A, 0xCE, 0xD4, 0xD5, 0xD6, 0xEA}

# Two-byte map 0F xx (no mandatory prefix unless listed in _OP2P)
_OP2 = {
    0x05: ("syscall", ""), 0x06: ("clts", ""), 0x07: ("sysret", ""), 0x08: ("invd", ""),
    0x09: ("wbinvd", ""), 0x0B: ("ud2", ""), 0x0D: ("prefetchw", "M"),
    0x18: ("prefetch", "M"), 0x19: ("nop", "Ev"), 0x1A: ("nop", "Ev"), 0x1B: ("nop", "Ev"),
    0x1C: ("nop", "Ev"), 0x1D: ("nop", "Ev"), 0x1E: ("nop", "Ev"), 0x1F: ("nop", "Ev"),
    0x20: ("mov", "Rq,Cq"), 0x21: ("mov", "Rq,Dq"), 0x22: ("mov", "Cq,Rq"), 0x23: ("mov", "Dq,Rq"),
    0x30: ("wrmsr", ""), 0x31: ("rdtsc", ""), 0x32: ("rdmsr", ""), 0x33: ("rdpmc", ""),
    0x34: ("sysenter", ""), 0x35: ("sysexit", ""), 0x37: ("getsec", ""),
    0xA0: ("push", "fs"), 0xA1: ("pop", "fs"), 0xA2: ("cpuid", ""), 0xA3: ("bt", "Ev,Gv"),
    0xA4: ("shld", "Ev,Gv,Ib"), 0xA5: ("shld", "Ev,Gv,CL"), 0xA8: ("push", "gs"), 0xA9: ("pop", "gs"),
    0xAA: ("rsm", ""), 0xAB: ("bts", "Ev,Gv"), 0xAC: ("shrd", "Ev,Gv,Ib"), 0xAD: ("shrd", "Ev,Gv,CL"),
    0xAF: ("imul", "Gv,Ev"), 0xB0: ("cmpxchg", "Eb,Gb"), 0xB1: ("cmpxchg", "Ev,Gv"),
    0xB2: ("lss", "Gv,M"), 0xB3: ("btr", "Ev,Gv"), 0xB4: ("lfs", "Gv,M"), 0xB5: ("lgs", "Gv,M"),
    0xB6: ("movzx", "Gv,Eb"), 0xB7: ("movzx", "Gv,Ew"), 0xB9: ("ud1", "Gv,Ev"),
    0xBB: ("btc", "Ev,Gv"), 0xBC: ("bsf", "Gv,Ev"), 0xBD: ("bsr", "Gv,Ev"),
    0xBE: ("movsx", "Gv,Eb"), 0xBF: ("movsx", "Gv,Ew"), 0xC0: ("xadd", "Eb,Gb"), 0xC1: ("xadd", "Ev,Gv"),
    0xC3: ("movnti", "My,Gy"), 0xFF: ("ud0", "Gv,Ev"),
}
for _c in range(16):
    _OP2[0x40 + _c] = ("cmov" + _CC[_c], "Gv,Ev")
    _OP2[0x80 + _c] = ("j" + _CC[_c], "Jz")
    _OP2[0x90 + _c] = ("set" + _CC[_c], "Eb")
for _r in range(8):
    _OP2[0xC8 + _r] = ("bswap", "Zy")

# SSE / MMX two-byte opcodes, selected by the last mandatory prefix (None, 66, F3, F2).
# Operands: V xmm reg, W xmm/mem, U xmm reg (rm), P/Q mmx, E/G gp, H VEX.vvvv, L ymm-able.
def _sse(table, op, none=None, p66=None, f3=None, f2=None):
    table[op] = {None: none, 0x66: p66, 0xF3: f3, 0xF2: f2}

_OP2P = {}
_sse(_OP2P, 0x10, ("movups", "Vx,Wx"), ("movupd", "Vx,Wx"), ("movss", "Vx,Wd"), ("movsd", "Vx,Wq"))
_sse(_OP2P, 0x11, ("movups", "Wx,Vx"), ("movupd", "Wx,Vx"), ("movss", "Wd,Vx"), ("movsd", "Wq,Vx"))
_sse(_OP2P, 0x12, ("movlps", "Vx,Wq"), ("movlpd", "Vx,Mq"), ("movsldup", "Vx,Wx"), ("movddup", "Vx,Wq"))
_sse(_OP2P, 0x13, ("movlps", "Mq,Vx"), ("movlpd", "Mq,Vx"))
_sse(_OP2P, 0x14, ("unpcklps", "Vx,Wx"), ("unpcklpd", "Vx,Wx"))
_sse(_OP2P, 0x15, ("unpckhps", "Vx,Wx"), ("unpckhpd", "Vx,Wx"))
_sse(_OP2P, 0x16, ("movhps", "Vx,Wq"), ("movhpd", "Vx,Mq"), ("movshdup", "Vx,Wx"))
_sse(_OP2P, 0x17, ("movhps", "Mq,Vx"), ("movhpd", "Mq,Vx"))
_sse(_OP2P, 0x28, ("movaps", "Vx,Wx"), ("movapd", "Vx,Wx"))
_sse(_OP2P, 0x29, ("movaps", "Wx,Vx"), ("movapd", "Wx,Vx"))
_sse(_OP2P, 0x2A, ("cvtpi2ps", "Vx,Qq"), ("cvtpi2pd", "Vx,Qq"), ("cvtsi2ss", "Vx,Ey"), ("cvtsi2sd", "Vx,Ey"))
_sse(_OP2P, 0x2B, ("movntps", "Mx,Vx"), ("movntpd", "Mx,Vx"))
_sse(_OP2P, 0x2C, ("cvttps2pi", "Pq,Wq"), ("cvttpd2pi", "Pq,Wx"), ("cvttss2si", "Gy,Wd"), ("cvttsd2si", "Gy,Wq"))
_sse(_OP2P, 0x2D, ("cvtps2pi", "Pq,Wq"), ("cvtpd2pi", "Pq,Wx"), ("cvtss2si", "Gy,Wd"), ("cvtsd2si", "Gy,Wq"))
_sse(_OP2P, 0x2E, ("ucomiss", "Vx,Wd"), ("ucomisd", "Vx,Wq"))
_sse(_OP2P, 0x2F, ("comiss", "Vx,Wd"), ("comisd", "Vx,Wq"))
_sse(_OP2P, 0x50, ("movmskps", "Gd,Ux"), ("movmskpd", "Gd,Ux"))
for _op, _n in ((0x51, "sqrt"), (0x58, "add"), (0x59, "mul"), (0x5C, "sub"), (0x5D, "min"),
                (0x5E, "div"), (0x5F, "max")):
    _sse(_OP2P, _op, (_n + "ps", "Vx,Wx"), (_n + "pd", "Vx,Wx"), (_n + "ss", "Vx,Wd"), (_n + "sd", "Vx,Wq"))
_sse(_OP2P, 0x52, ("rsqrtps", "Vx,Wx"), None, ("rsqrtss", "Vx,Wd"))
_sse(_OP2P, 0x53, ("rcpps", "Vx,Wx"), None, ("rcpss", "Vx,Wd"))
_sse(_OP2P, 0x54, ("andps", "Vx,Wx"), ("andpd", "Vx,Wx"))
_sse(_OP2P, 0x55, ("andnps", "Vx,Wx"), ("andnpd", "Vx,Wx"))
_sse(_OP2P, 0x56, ("orps", "Vx,Wx"), ("orpd", "Vx,Wx"))
_sse(_OP2P, 0x57, ("xorps", "Vx,Wx"), ("xorpd", "Vx,Wx"))
_sse(_OP2P, 0x5A, ("cvtps2pd", "Vx,Wq"), ("cvtpd2ps", "Vx,Wx"), ("cvtss2sd", "Vx,Wd"), ("cvtsd2ss", "Vx,Wq"))
_sse(_OP2P, 0x5B, ("cvtdq2ps", "Vx,Wx"), ("cvtps2dq", "Vx,Wx"), ("cvttps2dq", "Vx,Wx"))
_MMX = {0x60: "punpcklbw", 0x61: "punpcklwd", 0x62: "punpckldq", 0x63: "packsswb", 0x64: "pcmpgtb",
        0x65: "pcmpgtw", 0x66: "pcmpgtd", 0x67: "packuswb", 0x68: "punpckhbw", 0x69: "punpckhwd",
        0x6A: "punpckhdq", 0x6B: "packssdw", 0x74: "pcmpeqb", 0x75: "pcmpeqw", 0x76: "pcmpeqd",
        0xD1: "psrlw", 0xD2: "psrld", 0xD3: "psrlq", 0xD4: "paddq", 0xD5: "pmullw", 0xD8: "psubusb",
        0xD9: "psubusw", 0xDA: "pminub", 0xDB: "pand", 0xDC: "paddusb", 0xDD: "paddusw", 0xDE: "pmaxub",
        0xDF: "pandn", 0xE0: "pavgb", 0xE1: "psraw", 0xE2: "psrad", 0xE3: "pavgw", 0xE4: "pmulhuw",
        0xE5: "pmulhw", 0xE8: "psubsb", 0xE9: "psubsw", 0xEA: "pminsw", 0xEB: "por", 0xEC: "paddsb",
        0xED: "paddsw", 0xEE: "pmaxsw", 0xEF: "pxor", 0xF1: "psllw", 0xF2: "pslld", 0xF3: "psllq",
        0xF4: "pmuludq", 0xF5: "pmaddwd", 0xF6: "psadbw", 0xF8: "psubb", 0xF9: "psubw", 0xFA: "psubd",
        0xFB: "psubq", 0xFC: "paddb", 0xFD: "paddw", 0xFE: "paddd"}
for _op, _n in _MMX.items():
    _sse(_OP2P, _op, (_n, "Pq,Qq"), (_n, "Vx,Wx"))
_sse(_OP2P, 0x6C, None, ("punpcklqdq", "Vx,Wx"))
_sse(_OP2P, 0x6D, None, ("punpckhqdq", "Vx,Wx"))
_sse(_OP2P, 0x6E, ("movd", "Pq,Ey"), ("movd", "Vx,Ey"))
_sse(_OP2P, 0x6F, ("movq", "Pq,Qq"), ("movdqa", "Vx,Wx"), ("movdqu", "Vx,Wx"))
_sse(_OP2P, 0x70, ("pshufw", "Pq,Qq,Ib"), ("pshufd", "Vx,Wx,Ib"), ("pshufhw", "Vx,Wx,Ib"), ("pshuflw", "Vx,Wx,Ib"))
_sse(_OP2P, 0x77, ("emms", ""))
_sse(_OP2P, 0x78, ("vmread", "Eq,Gq"))
_sse(_OP2P, 0x79, ("vmwrite", "Gq,Eq"))
_sse(_OP2P, 0x7C, None, ("haddpd", "Vx,Wx"), None, ("haddps", "Vx,Wx"))
_sse(_OP2P, 0x7D, None, ("hsubpd", "Vx,Wx"), None, ("hsubps", "Vx,Wx"))
_sse(_OP2P, 0x7E, ("movd", "Ey,Pq"), ("movd", "Ey,Vx"), ("movq", "Vx,Wq"))
_sse(_OP2P, 0x7F, ("movq", "Qq,Pq"), ("movdqa", "Wx,Vx"), ("movdqu", "Wx,Vx"))
_sse(_OP2P, 0xB8, None, None, ("popcnt", "Gv,Ev"))
_sse(_OP2P, 0xBC, ("bsf", "Gv,Ev"), ("bsf", "Gv,Ev"), ("tzcnt", "Gv,Ev"), ("bsf", "Gv,Ev"))
_sse(_OP2P, 0xBD, ("bsr", "Gv,Ev"), ("bsr", "Gv,Ev"), ("lzcnt", "Gv,Ev"), ("bsr", "Gv,Ev"))
_sse(_OP2P, 0xC2, ("cmpps", "Vx,Wx,Ib"), ("cmppd", "Vx,Wx,Ib"), ("cmpss", "Vx,Wd,Ib"), ("cmpsd", "Vx,Wq,Ib"))
_sse(_OP2P, 0xC4, ("pinsrw", "Pq,Ed,Ib"), ("pinsrw", "Vx,Ed,Ib"))
_sse(_OP2P, 0xC5, ("pextrw", "Gd,Nq,Ib"), ("pextrw", "Gd,Ux,Ib"))
_sse(_OP2P, 0xC6, ("shufps", "Vx,Wx,Ib"), ("shufpd", "Vx,Wx,Ib"))
_sse(_OP2P, 0xD0, None, ("addsubpd", "Vx,Wx"), None, ("addsubps", "Vx,Wx"))
_sse(_OP2P, 0xD6, None, ("movq", "Wq,Vx"), ("movq2dq", "Vx,Nq"), ("movdq2q", "Pq,Ux"))
_sse(_OP2P, 0xD7, ("pmovmskb", "Gd,Nq"), ("pmovmskb", "Gd,Ux"))
_sse(_OP2P, 0xE6, None, ("cvttpd2dq", "Vx,Wx"), ("cvtdq2pd", "Vx,Wq"), ("cvtpd2dq", "Vx,Wx"))
_sse(_OP2P, 0xE7, ("movntq", "Mq,Pq"), ("movntdq", "Mx,Vx"))
_sse(_OP2P, 0xF0, None, None, None, ("lddqu", "Vx,Mx"))
_sse(_OP2P, 0xF7, ("maskmovq", "Pq,Nq"), ("maskmovdqu", "Vx,Ux"))
# shift-by-immediate groups 12/13/14 (0F 71/72/73): mmx (no prefix) or xmm (66)
_SHIFTGRP = {0x71: {2: "psrlw", 4: "psraw", 6: "psllw"},
             0x72: {2: "psrld", 4: "psrad", 6: "pslld"},
             0x73: {2: "psrlq", 3: "psrldq", 6: "psllq", 7: "pslldq"}}

# 0F 38 xx (SSSE3/SSE4/AES/SHA...): mnemonic by prefix; operands Vx,Wx unless noted
_OP38 = {}
for _op, _n in {0x00: "pshufb", 0x01: "phaddw", 0x02: "phaddd", 0x03: "phaddsw", 0x04: "pmaddubsw",
                0x05: "phsubw", 0x06: "phsubd", 0x07: "phsubsw", 0x08: "psignb", 0x09: "psignw",
                0x0A: "psignd", 0x0B: "pmulhrsw", 0x1C: "pabsb", 0x1D: "pabsw", 0x1E: "pabsd"}.items():
    _sse(_OP38, _op, (_n, "Pq,Qq"), (_n, "Vx,Wx"))
for _op, _n in {0x10: "pblendvb", 0x14: "blendvps", 0x15: "blendvpd", 0x17: "ptest", 0x20: "pmovsxbw",
                0x21: "pmovsxbd", 0x22: "pmovsxbq", 0x23: "pmovsxwd", 0x24: "pmovsxwq", 0x25: "pmovsxdq",
                0x28: "pmuldq", 0x29: "pcmpeqq", 0x2A: "movntdqa", 0x2B: "packusdw", 0x30: "pmovzxbw",
                0x31: "pmovzxbd", 0x32: "pmovzxbq", 0x33: "pmovzxwd", 0x34: "pmovzxwq", 0x35: "pmovzxdq",
                0x37: "pcmpgtq", 0x38: "pminsb", 0x39: "pminsd", 0x3A: "pminuw", 0x3B: "pminud",
                0x3C: "pmaxsb", 0x3D: "pmaxsd", 0x3E: "pmaxuw", 0x3F: "pmaxud", 0x40: "pmulld",
                0x41: "phminposuw", 0x80: "invept", 0x81: "invvpid", 0x82: "invpcid", 0xDB: "aesimc",
                0xDC: "aesenc", 0xDD: "aesenclast", 0xDE: "aesdec", 0xDF: "aesdeclast"}.items():
    _sse(_OP38, _op, None, (_n, "Gq,M" if _op in (0x80, 0x81, 0x82) else "Vx,Wx"))
for _op, _n in {0xC8: "sha1nexte", 0xC9: "sha1msg1", 0xCA: "sha1msg2", 0xCB: "sha256rnds2",
                0xCC: "sha256msg1", 0xCD: "sha256msg2"}.items():
    _sse(_OP38, _op, (_n, "Vx,Wx"))
_sse(_OP38, 0xF0, ("movbe", "Gv,M"), ("movbe", "Gv,M"), None, ("crc32", "Gd,Eb"))
_sse(_OP38, 0xF1, ("movbe", "M,Gv"), ("movbe", "M,Gv"), None, ("crc32", "Gd,Ev"))
_sse(_OP38, 0xF5, None, ("wruss", "My,Gy"))
_sse(_OP38, 0xF6, ("wrss", "My,Gy"), ("adcx", "Gy,Ey"), ("adox", "Gy,Ey"))
_sse(_OP38, 0xF8, None, ("movdir64b", "Gq,M"), ("enqcmds", "Gq,M"), ("enqcmd", "Gq,M"))
_sse(_OP38, 0xF9, ("movdiri", "My,Gy"))
# 0F 3A xx: all take an imm8
_OP3A = {}
for _op, _n in {0x08: "roundps", 0x09: "roundpd", 0x0A: "roundss", 0x0B: "roundsd", 0x0C: "blendps",
                0x0D: "blendpd", 0x0E: "pblendw", 0x14: "pextrb", 0x15: "pextrw", 0x16: "pextrd",
                0x17: "extractps", 0x20: "pinsrb", 0x21: "insertps", 0x22: "pinsrd", 0x40: "dpps",
                0x41: "dppd", 0x42: "mpsadbw", 0x44: "pclmulqdq", 0x60: "pcmpestrm", 0x61: "pcmpestri",
                0x62: "pcmpistrm", 0x63: "pcmpistri", 0xDF: "aeskeygenassist"}.items():
    _ops = "Ey,Vx,Ib" if _op in (0x14, 0x15, 0x16, 0x17) else ("Vx,Ey,Ib" if _op in (0x20, 0x22) else "Vx,Wx,Ib")
    _sse(_OP3A, _op, None, (_n, _ops))
_sse(_OP3A, 0x0F, ("palignr", "Pq,Qq,Ib"), ("palignr", "Vx,Wx,Ib"))
_sse(_OP3A, 0xCC, ("sha1rnds4", "Vx,Wx,Ib"))

# VEX-only opcodes (AVX/AVX2/FMA/BMI) that have no legacy SSE twin: (map, op) -> {pp: (name, ops)}
_VEXONLY = {
    (1, 0x77): {0: ("vzeroupper", "")},
    (2, 0x0C): {1: ("vpermilps", "Vx,Hx,Wx")}, (2, 0x0D): {1: ("vpermilpd", "Vx,Hx,Wx")},
    (2, 0x16): {1: ("vpermps", "Vx,Hx,Wx")}, (2, 0x18): {1: ("vbroadcastss", "Vx,Wd")},
    (2, 0x19): {1: ("vbroadcastsd", "Vx,Wq")}, (2, 0x1A): {1: ("vbroadcastf128", "Vx,M")},
    (2, 0x2C): {1: ("vmaskmovps", "Vx,Hx,M")}, (2, 0x2E): {1: ("vmaskmovps", "M,Hx,Vx")},
    (2, 0x36): {1: ("vpermd", "Vx,Hx,Wx")}, (2, 0x45): {1: ("vpsrlvd", "Vx,Hx,Wx")},
    (2, 0x46): {1: ("vpsravd", "Vx,Hx,Wx")}, (2, 0x47): {1: ("vpsllvd", "Vx,Hx,Wx")},
    (2, 0x58): {1: ("vpbroadcastd", "Vx,Wd")}, (2, 0x59): {1: ("vpbroadcastq", "Vx,Wq")},
    (2, 0x5A): {1: ("vbroadcasti128", "Vx,M")}, (2, 0x78): {1: ("vpbroadcastb", "Vx,Wb")},
    (2, 0x79): {1: ("vpbroadcastw", "Vx,Ww")}, (2, 0x8C): {1: ("vpmaskmovd", "Vx,Hx,M")},
    (2, 0x8E): {1: ("vpmaskmovd", "M,Hx,Vx")},
    (2, 0xF2): {0: ("andn", "Gy,By,Ey")}, (2, 0xF5): {0: ("bzhi", "Gy,Ey,By"), 3: ("pdep", "Gy,By,Ey"), 2: ("pext", "Gy,By,Ey")},
    (2, 0xF6): {3: ("mulx", "Gy,By,Ey")}, (2, 0xF7): {0: ("bextr", "Gy,Ey,By"), 1: ("shlx", "Gy,Ey,By"),
                                                      2: ("sarx", "Gy,Ey,By"), 3: ("shrx", "Gy,Ey,By")},
    (3, 0x00): {1: ("vpermq", "Vx,Wx,Ib")}, (3, 0x01): {1: ("vpermpd", "Vx,Wx,Ib")},
    (3, 0x02): {1: ("vpblendd", "Vx,Hx,Wx,Ib")}, (3, 0x04): {1: ("vpermilps", "Vx,Wx,Ib")},
    (3, 0x05): {1: ("vpermilpd", "Vx,Wx,Ib")}, (3, 0x06): {1: ("vperm2f128", "Vx,Hx,Wx,Ib")},
    (3, 0x18): {1: ("vinsertf128", "Vx,Hx,Wx,Ib")}, (3, 0x19): {1: ("vextractf128", "Wx,Vx,Ib")},
    (3, 0x38): {1: ("vinserti128", "Vx,Hx,Wx,Ib")}, (3, 0x39): {1: ("vextracti128", "Wx,Vx,Ib")},
    (3, 0x46): {1: ("vperm2i128", "Vx,Hx,Wx,Ib")}, (3, 0x4A): {1: ("vblendvps", "Vx,Hx,Wx,Lx")},
    (3, 0x4B): {1: ("vblendvpd", "Vx,Hx,Wx,Lx")}, (3, 0x4C): {1: ("vpblendvb", "Vx,Hx,Wx,Lx")},
    (3, 0xF0): {3: ("rorx", "Gy,Ey,Ib")},
}
for _base, _n in ((0x96, "fmaddsub"), (0x97, "fmsubadd"), (0x98, "fmadd"), (0x9A, "fmsub"),
                  (0x9C, "fnmadd"), (0x9E, "fnmsub")):
    for _form, _off in (("132", 0), ("213", 0x10), ("231", 0x20)):
        _VEXONLY[(2, _base + _off)] = {1: ("v" + _n + _form + "ps", "Vx,Hx,Wx")}
        if _base >= 0x98:
            _VEXONLY[(2, _base + _off + 1)] = {1: ("v" + _n + _form + "ss", "Vx,Hx,Wx")}

# x87 escape opcodes D8-DF: memory forms by modrm.reg, and a few register forms
_X87_MEM = {
    0xD8: ["fadd", "fmul", "fcom", "fcomp", "fsub", "fsubr", "fdiv", "fdivr"],
    0xD9: ["fld", None, "fst", "fstp", "fldenv", "fldcw", "fnstenv", "fnstcw"],
    0xDA: ["fiadd", "fimul", "ficom", "ficomp", "fisub", "fisubr", "fidiv", "fidivr"],
    0xDB: ["fild", "fisttp", "fist", "fistp", None, "fld", None, "fstp"],
    0xDC: ["fadd", "fmul", "fcom", "fcomp", "fsub", "fsubr", "fdiv", "fdivr"],
    0xDD: ["fld", "fisttp", "fst", "fstp", "frstor", None, "fnsave", "fnstsw"],
    0xDE: ["fiadd", "fimul", "ficom", "ficomp", "fisub", "fisubr", "fidiv", "fidivr"],
    0xDF: ["fild", "fisttp", "fist", "fistp", "fbld", "fild", "fbstp", "fistp"],
}
_X87_MEMSIZE = {0xD8: 4, 0xD9: 4, 0xDA: 4, 0xDB: 4, 0xDC: 8, 0xDD: 8, 0xDE: 2, 0xDF: 2}
_X87_REG = {0xD9E0: "fchs", 0xD9E1: "fabs", 0xD9E4: "ftst", 0xD9E5: "fxam", 0xD9E8: "fld1",
            0xD9EE: "fldz", 0xD9F0: "f2xm1", 0xD9FA: "fsqrt", 0xD9FC: "frndint", 0xD9FE: "fsin",
            0xD9FF: "fcos", 0xDBE2: "fnclex", 0xDBE3: "fninit", 0xDFE0: "fnstsw ax", 0xD9D0: "fnop"}


class Insn:
    """One decoded instruction."""
    __slots__ = ("address", "length", "mnemonic", "operands", "raw", "target", "kind", "rip_ref")

    def __init__(self, address, length, mnemonic, operands, raw, target=None, kind=None, rip_ref=None):
        self.address = address
        self.length = length
        self.mnemonic = mnemonic
        self.operands = operands
        self.raw = raw
        self.target = target      # branch/call destination (relative or rip-relative pointer)
        self.kind = kind          # "call", "jmp", "jcc", "ret", or None
        self.rip_ref = rip_ref    # absolute address of a rip-relative memory operand

    @property
    def text(self) -> str:
        return f"{self.mnemonic:<7} {self.operands}".rstrip() if self.operands else self.mnemonic


def _imm_text(v: int) -> str:
    """WinDbg style hex immediate: 28h, 0FFh."""
    s = f"{v:X}"
    return ("0" + s if s[0] in "ABCDEF" else s) + "h" if v > 9 else str(v)


class X64Disassembler:
    """Decodes x86-64 (long mode) instructions. Covers the general purpose, system,
    x87, MMX/SSE1-4.2, AES/SHA, AVX/AVX2/FMA/BMI (VEX) instruction sets; EVEX
    (AVX-512) is decoded for length with a generic mnemonic."""

    def decode(self, code: bytes, address: int) -> Insn:
        try:
            return _Decoder(code, address).run()
        except (IndexError, _Invalid):
            return Insn(address, 1, "???", "", code[:1])

    def decode_all(self, code: bytes, address: int, count: int | None = None):
        off = 0
        out = []
        while off < len(code) and (count is None or len(out) < count):
            ins = self.decode(code[off:off + 15], address + off)
            out.append(ins)
            off += ins.length
        return out


class _Invalid(Exception):
    pass


class _Decoder:
    def __init__(self, code, address):
        self.c = code
        self.addr = address
        self.p = 0
        self.rex = 0
        self.opsize16 = False
        self.addr32 = False
        self.seg = None
        self.rep = None          # 0xF2 / 0xF3, last one wins
        self.lock = False
        self.mandatory = None    # mandatory SSE prefix (66/F2/F3)
        self.vex = None          # dict when VEX/EVEX encoded
        self.modrm = None
        self.rip_disp_at = None
        self.mem_text = None
        self.target = None
        self.kind = None
        self.rip_ref = None

    # -- byte access -- #
    def u8(self):
        v = self.c[self.p]
        self.p += 1
        return v

    def sx(self, n):
        v = int.from_bytes(self.c[self.p:self.p + n], "little", signed=True)
        if self.p + n > len(self.c):
            raise IndexError
        self.p += n
        return v

    def ux(self, n):
        if self.p + n > len(self.c):
            raise IndexError
        v = int.from_bytes(self.c[self.p:self.p + n], "little")
        self.p += n
        return v

    # -- prefixes -- #
    def run(self) -> Insn:
        prefixes = []
        while True:
            b = self.c[self.p]
            if b in (0xF0, 0xF2, 0xF3, 0x2E, 0x36, 0x3E, 0x26, 0x64, 0x65, 0x66, 0x67):
                self.p += 1
                prefixes.append(b)
                if b == 0x66:
                    self.opsize16 = True
                elif b == 0x67:
                    self.addr32 = True
                elif b in (0xF2, 0xF3):
                    self.rep = b
                elif b == 0xF0:
                    self.lock = True
                elif b in (0x64, 0x65):
                    self.seg = "fs" if b == 0x64 else "gs"
                if len(prefixes) > 14:
                    raise _Invalid
                continue
            break
        # the last of 66/F2/F3 acts as the mandatory prefix of SSE instructions
        for b in reversed(prefixes):
            if b in (0x66, 0xF2, 0xF3):
                self.mandatory = b
                break
        b = self.c[self.p]
        if 0x40 <= b <= 0x4F:
            self.rex = b
            self.p += 1
            b = self.c[self.p]
        self.p += 1
        if b in (0xC4, 0xC5, 0x62) and not self.rex:
            return self._vex(b)
        if b == 0x0F:
            return self._two_byte()
        return self._one_byte(b)

    # -- helpers -- #
    @property
    def rex_w(self):
        return bool(self.rex & 8)

    def opsize(self, d64=False):
        if self.rex_w:
            return 8
        if self.opsize16:
            return 2
        return 8 if d64 else 4

    def reg_name(self, n, size):
        if size == 8:
            return _GPR64[n]
        if size == 4:
            return _GPR32[n]
        if size == 2:
            return _GPR16[n]
        if size == 1:
            return _GPR8_REX[n] if (self.rex or n >= 8 or self.vex) else _GPR8_LEGACY[n]
        raise _Invalid

    def read_modrm(self):
        if self.modrm is None:
            m = self.u8()
            mod, reg, rm = m >> 6, (m >> 3) & 7, m & 7
            self.modrm = (mod, reg, rm)
            if mod != 3:
                self._mem_operand(mod, rm)
        return self.modrm

    def reg_field(self):
        return self.modrm[1] | (8 if self.rex & 4 else 0)

    def rm_field(self):
        return self.modrm[2] | (8 if self.rex & 1 else 0)

    def _mem_operand(self, mod, rm):
        areg = _GPR32 if self.addr32 else _GPR64
        base = index = None
        scale = 1
        disp = 0
        rip = False
        if rm == 4:
            sib = self.u8()
            scale = 1 << (sib >> 6)
            idx = ((sib >> 3) & 7) | (8 if self.rex & 2 else 0)
            b = (sib & 7) | (8 if self.rex & 1 else 0)
            if idx != 4:
                index = areg[idx]
            if (sib & 7) == 5 and mod == 0:
                disp = self.sx(4)
            else:
                base = areg[b]
        elif rm == 5 and mod == 0:
            rip = True
            self.rip_disp_at = self.p
            disp = self.sx(4)
        else:
            base = areg[rm | (8 if self.rex & 1 else 0)]
        if mod == 1:
            disp = self.sx(1)
        elif mod == 2:
            disp = self.sx(4)
        self.mem = (base, index, scale, disp, rip)

    def mem_text_for(self, size):
        base, index, scale, disp, rip = self.mem
        if rip:
            inner = None            # filled once the instruction length is known
        else:
            parts = []
            if base:
                parts.append(base)
            if index:
                parts.append(index + (f"*{scale}" if scale > 1 else ""))
            s = "+".join(parts)
            if disp or not s:
                if s:
                    s += ("-" if disp < 0 else "+") + _imm_text(abs(disp))
                else:
                    s = _imm_text(disp & 0xFFFFFFFFFFFFFFFF)
            inner = s
        seg = f"{self.seg}:" if self.seg else ""
        prefix = f"{_MEMSIZE[size]} ptr " if size in _MEMSIZE else ""
        return (prefix, seg, inner)

    # -- operand formatting -- #
    def operands(self, spec, size_default=None):
        """Render an operand spec string ("Ev,Gv"...) and consume its bytes."""
        if not spec:
            return []
        out = []
        needs_modrm = any(t[0] in "EGMRSCDVWUPQNH" and t not in ("CL",) for t in spec.split(",")
                          if t not in ("1", "AL", "rAX", "eAX", "DX", "CL", "3"))
        if needs_modrm:
            self.read_modrm()
        for t in spec.split(","):
            out.append(self.operand(t))
        return out

    def size_of(self, code):
        osz = self.opsize()
        return {"b": 1, "w": 2, "d": 4, "q": 8, "v": osz, "z": 2 if osz == 2 else 4,
                "y": 8 if self.rex_w else 4, "x": (16, 32, 64, 64)[self.vex["L"] if self.vex else 0],
                "": osz}[code]

    def operand(self, t):
        kind, sz = t[0], t[1:]
        if t == "1":
            return "1"
        if t == "3":
            return "3"
        if t == "AL":
            return "al"
        if t == "CL":
            return "cl"
        if t == "DX":
            return "dx"
        if t in ("rAX", "eAX"):
            return self.reg_name(0, self.opsize() if t == "rAX" else (2 if self.opsize16 else 4))
        if kind in "E" and t not in ("Ev64",):
            size = self.size_of(sz)
            return self.rm_operand(size)
        if t == "Ev64":
            return self.rm_operand(self.opsize(d64=True))
        if kind == "M":
            size = {"q": 8, "x": (16, 32, 64, 64)[self.vex["L"] if self.vex else 0], "y": 8 if self.rex_w else 4,
                    "": 0}.get(sz, 0)
            if self.modrm[0] == 3:
                raise _Invalid
            return self.rm_operand(size)
        if kind == "G":
            return self.reg_name(self.reg_field(), self.size_of(sz))
        if kind == "R":
            return _GPR64[self.rm_field()]
        if kind == "C":
            return f"cr{self.reg_field()}"
        if kind == "D":
            return f"dr{self.reg_field()}"
        if kind == "S":
            return _SEG[self.modrm[1]]
        if kind == "Z":
            n = (self.op & 7) | (8 if self.rex & 1 else 0)
            if sz == "v64":
                return self.reg_name(n, self.opsize(d64=True))
            if sz == "y":
                return self.reg_name(n, 8 if self.rex_w else 4)
            return self.reg_name(n, self.size_of(sz))
        if kind == "I":
            if sz == "b":
                v = self.ux(1)
            elif sz == "b64":       # push imm8: sign-extended to the stack width
                v = self.sx(1) & 0xFFFFFFFFFFFFFFFF
            elif sz == "bs":        # imm8 sign-extended to the operand size (83 /x, 6B)
                v = self.sx(1) & ((1 << (self.opsize() * 8)) - 1)
            elif sz == "w":
                v = self.ux(2)
            elif sz in ("z", "z64"):
                n = 2 if self.opsize16 else 4
                v = self.sx(n)
                width = self.opsize(d64=(sz == "z64"))
                v &= (1 << (width * 8)) - 1
            elif sz == "v":
                n = self.opsize()
                v = self.ux(n)
            else:
                raise _Invalid
            return _imm_text(v)
        if kind == "J":
            n = 1 if sz == "b" else (2 if self.opsize16 else 4)
            rel = self.sx(n)
            self.target = ("rel", rel)
            return None   # resolved later
        if kind == "O":
            moff = self.ux(4 if self.addr32 else 8)
            size = 1 if sz == "b" else self.opsize()
            seg = f"{self.seg}:" if self.seg else ""
            return f"{_MEMSIZE[size]} ptr {seg}[{_imm_text(moff)}]"
        if kind in "VWUHL":
            return self.xmm_operand(kind, sz)
        if kind in "PQN":
            if kind == "P":
                return f"mm{self.modrm[1]}"
            if self.modrm[0] == 3:
                return f"mm{self.modrm[2]}"
            return self.rm_operand(8)
        if kind == "B":   # VEX.vvvv general register (BMI)
            return self.reg_name(self.vex["vvvv"], 8 if self.rex_w else 4)
        raise _Invalid

    def xmm_operand(self, kind, sz):
        width = self.vex["L"] if self.vex else 0
        wide = bool(width)
        reg = ("xmm", "ymm", "zmm", "zmm")[width] if sz == "x" else "xmm"
        if kind == "V":
            return f"{reg}{self.reg_field()}"
        if kind == "H":
            return f"{reg}{self.vex['vvvv']}"
        if kind == "L":   # register in imm8[7:4]
            v = self.ux(1)
            return f"{reg}{v >> 4}"
        if kind == "U" or self.modrm[0] == 3:
            return f"{reg}{self.rm_field()}"
        size = {"x": (16, 32, 64, 64)[width], "q": 8, "d": 4, "w": 2, "b": 1}.get(sz, 16)
        return self.rm_operand(size)

    def rm_operand(self, size):
        mod = self.modrm[0]
        if mod == 3:
            return self.reg_name(self.rm_field(), size if size in (1, 2, 4, 8) else 8)
        return ("MEM", self.mem_text_for(size))

    # -- finishing -- #
    def finish(self, mnemonic, ops, kind=None):
        length = self.p
        if length > 15:
            raise _Invalid
        end = self.addr + length
        text_ops = []
        for o in ops:
            if o is None:     # relative branch target
                rel = self.target[1]
                dest = (end + rel) & 0xFFFFFFFFFFFFFFFF
                self.target = dest
                text_ops.append(f"{dest:016x}")
            elif isinstance(o, tuple) and o[0] == "MEM":
                prefix, seg, inner = o[1]
                if inner is None:
                    ref = (end + self.mem[3]) & 0xFFFFFFFFFFFFFFFF
                    self.rip_ref = ref
                    inner = f"{ref:016x}"
                text_ops.append(f"{prefix}{seg}[{inner}]")
            else:
                text_ops.append(o)
        if isinstance(self.target, tuple):
            self.target = None
        if self.rip_ref is not None and kind in ("call", "jmp") and self.target is None:
            self.target = self.rip_ref     # call/jmp qword ptr [rip+x]: pointer to the target
        pre = ""
        if self.lock:
            pre = "lock "
        if self.rep and mnemonic in ("movs", "stos", "lods", "ins", "outs", "movsb", "movsw", "movsd",
                                     "movsq", "stosb", "stosw", "stosd", "stosq", "lodsb", "lodsw",
                                     "lodsd", "lodsq", "insb", "outsb"):
            pre += "rep "
        elif self.rep and mnemonic in ("cmps", "scas", "cmpsb", "cmpsw", "cmpsd", "cmpsq", "scasb",
                                       "scasw", "scasd", "scasq"):
            pre += "repe " if self.rep == 0xF3 else "repne "
        return Insn(self.addr, length, pre + mnemonic, ",".join(text_ops), bytes(self.c[:length]),
                    self.target, kind, self.rip_ref)

    # -- one-byte map -- #
    def _one_byte(self, op):
        self.op = op
        if op in _INVALID64:
            raise _Invalid
        if op in _GRP:
            names, spec = _GRP[op]
            self.read_modrm()
            name = names[self.modrm[1]]
            if op == 0xC7 and self.modrm == (3, 7, 0):
                return self.finish("xbegin", [self.operand("Jz")])
            if op == 0xC6 and self.modrm == (3, 7, 0):
                return self.finish("xabort", [self.operand("Ib")])
            if name is None:
                raise _Invalid
            if op in (0xF6, 0xF7):
                if self.modrm[1] in (0, 1):
                    spec = spec + (",Ib" if op == 0xF6 else ",Iz")
            if op == 0xFF:
                r = self.modrm[1]
                if r in (2, 4):
                    kind = "call" if r == 2 else "jmp"
                    return self.finish(name, [self.rm_operand(8)], kind)
                if r in (3, 5):
                    if self.modrm[0] == 3:
                        raise _Invalid
                    return self.finish(name, [self.rm_operand(10 if self.rex_w else 6)],
                                       "call" if r == 3 else "jmp")
                if r == 6:
                    return self.finish(name, [self.rm_operand(self.opsize(d64=True))])
            return self.finish(name, self.operands(spec))
        if 0xD8 <= op <= 0xDF:
            return self._x87(op)
        if op not in _OP1:
            raise _Invalid
        name, spec = _OP1[op]
        kind = None
        if op == 0x90:
            if self.rex & 1:
                return self.finish("xchg", ["r8", "rax"] if self.rex_w else ["r8d", "eax"])
            if self.rep == 0xF3:
                return self.finish("pause", [])
            return self.finish("nop", [])
        if spec == "cbw":
            name = {2: "cbw", 4: "cwde", 8: "cdqe"}[self.opsize()]
            spec = ""
        elif spec == "cwd":
            name = {2: "cwd", 4: "cdq", 8: "cqo"}[self.opsize()]
            spec = ""
        elif spec == "f64":
            name = name + ("" if self.opsize16 else "q")
            spec = ""
        elif spec == "iret":
            name = {2: "iret", 4: "iretd", 8: "iretq"}[self.opsize()]
            spec = ""
        elif spec in ("Xb", "Xv"):
            size = 1 if spec == "Xb" else self.opsize()
            name = name + {1: "b", 2: "w", 4: "d", 8: "q"}[size]
            spec = ""
        elif spec == "sz":
            name = name + ("w" if self.opsize16 else "d")
            spec = ""
        if op == 0x63 and not self.rex_w:
            spec = "Gv,Ed"
        if name in ("call",):
            kind = "call"
        elif name == "jmp":
            kind = "jmp"
        elif name.startswith("j") or name in ("loop", "loope", "loopne", "jrcxz"):
            kind = "jcc"
            if op == 0xE3 and self.addr32:
                name = "jecxz"
        elif name in ("ret", "retf", "iret", "iretd", "iretq"):
            kind = "ret"
        if spec in ("Zv64",):
            spec = "Zv64"
        ops = self.operands(spec.replace("Iz64", "Iz64").replace("Ib64", "Ib64"))
        return self.finish(name, ops, kind)

    def _x87(self, op):
        mod, reg, rm = self.read_modrm()
        if mod != 3:
            name = _X87_MEM[op][reg]
            if name is None:
                raise _Invalid
            size = _X87_MEMSIZE[op]
            if op == 0xDB and reg in (5, 7):
                size = 10
            elif op == 0xDD and reg in (4, 6):
                size = 0      # frstor/fnsave: 108-byte state
            elif op == 0xDF and reg in (4, 6):
                size = 10
            elif op == 0xDF and reg in (5, 7):
                size = 8
            elif op == 0xD9 and reg in (4, 5, 6, 7):
                size = 2 if reg in (5, 7) else 0
            elif op == 0xDD and reg == 7:
                size = 2
            return self.finish(name, [self.rm_operand(size)])
        key = (op << 8) | (0xC0 | (reg << 3) | rm)
        if key in _X87_REG:
            parts = _X87_REG[key].split(" ")
            return self.finish(parts[0], parts[1:])
        st = f"st({rm})"
        tables = {
            0xD8: ["fadd", "fmul", "fcom", "fcomp", "fsub", "fsubr", "fdiv", "fdivr"],
            0xDC: ["fadd", "fmul", "fcom", "fcomp", "fsubr", "fsub", "fdivr", "fdiv"],
            0xDE: ["faddp", "fmulp", None, "fcompp", "fsubrp", "fsubp", "fdivrp", "fdivp"],
            0xD9: ["fld", "fxch", None, None, None, None, None, None],
            0xDD: ["ffree", None, "fst", "fstp", "fucom", "fucomp", None, None],
            0xDA: ["fcmovb", "fcmove", "fcmovbe", "fcmovu", None, "fucompp", None, None],
            0xDB: ["fcmovnb", "fcmovne", "fcmovnbe", "fcmovnu", None, "fucomi", "fcomi", None],
            0xDF: [None, None, None, None, None, "fucomip", "fcomip", None],
        }
        name = tables.get(op, [None] * 8)[reg]
        if name is None:
            return self.finish("fpu", [f"{op:02X}{0xC0 | (reg << 3) | rm:02X}h"])
        if op in (0xD8, 0xDA, 0xDB, 0xDF) and name.startswith(("fcmov", "fcomi", "fucomi", "fadd", "fmul",
                                                                    "fsub", "fdiv", "fcom")):
            return self.finish(name, ["st", st] if name not in ("fcom", "fcomp") else [st])
        if op in (0xDC, 0xDE):
            return self.finish(name, [st, "st"])
        return self.finish(name, [st])

    # -- two-byte map -- #
    def _two_byte(self):
        op = self.u8()
        self.op = op
        if op == 0x38:
            return self._three_byte(0x38, self.u8())
        if op == 0x3A:
            return self._three_byte(0x3A, self.u8())
        if op == 0x00:
            mod, reg, rm = self.read_modrm()
            names = ["sldt", "str", "lldt", "ltr", "verr", "verw", None, None]
            if names[reg] is None:
                raise _Invalid
            return self.finish(names[reg], [self.rm_operand(2 if mod != 3 or reg >= 2 else self.opsize())])
        if op == 0x01:
            return self._grp7()
        if op in (0x02, 0x03):
            return self.finish("lar" if op == 2 else "lsl", self.operands("Gv,Ew"))
        if op == 0x0D:
            mod, reg, rm = self.read_modrm()
            return self.finish("prefetchw" if reg == 1 else "prefetch", [self.rm_operand(1)])
        if op == 0x18:
            mod, reg, rm = self.read_modrm()
            name = ["prefetchnta", "prefetcht0", "prefetcht1", "prefetcht2"][reg] if reg < 4 else "nop"
            return self.finish(name, [self.rm_operand(1 if reg < 4 else 4)])
        if op == 0x1E and self.rep == 0xF3:
            mod, reg, rm = self.read_modrm()
            if self.modrm == (3, 7, 2):
                return self.finish("endbr64", [])
            if self.modrm == (3, 7, 3):
                return self.finish("endbr32", [])
            if mod == 3 and reg == 1:
                return self.finish("rdsspq" if self.rex_w else "rdsspd", [self.reg_name(self.rm_field(), 8 if self.rex_w else 4)])
            return self.finish("nop", [self.rm_operand(self.opsize())])
        if op == 0xAE:
            return self._grp15()
        if op == 0xBA:
            mod, reg, rm = self.read_modrm()
            names = [None, None, None, None, "bt", "bts", "btr", "btc"]
            if names[reg] is None:
                raise _Invalid
            return self.finish(names[reg], [self.rm_operand(self.opsize()), self.operand("Ib")])
        if op == 0xC7:
            return self._grp9()
        if op in _SHIFTGRP:
            mod, reg, rm = self.read_modrm()
            name = _SHIFTGRP[op].get(reg)
            if name is None or mod != 3:
                raise _Invalid
            reg_txt = f"xmm{self.rm_field()}" if self.mandatory == 0x66 else f"mm{rm}"
            return self.finish(name, [reg_txt, self.operand("Ib")])
        if op in (0x10, 0x11, 0x12, 0x13, 0x16, 0x17) or op in _OP2P:
            entry = _OP2P.get(op, {}).get(self._sel_prefix(op))
            if entry is None:
                entry = _OP2P.get(op, {}).get(None)
            if entry is None:
                raise _Invalid
            name, spec = entry
            if op == 0x12 and self.mandatory is None:
                self.read_modrm()
                if self.modrm[0] == 3:
                    name = "movhlps"
            if op == 0x16 and self.mandatory is None:
                self.read_modrm()
                if self.modrm[0] == 3:
                    name = "movlhps"
            if op in (0x6E, 0x7E) and self.rex_w:
                name = "movq"
            return self.finish(name, self.operands(spec))
        if op in _OP2:
            name, spec = _OP2[op]
            kind = "jcc" if (0x80 <= op <= 0x8F) else ("ret" if op == 0x07 else None)
            if op == 0x07:
                name = "sysretq" if self.rex_w else "sysret"
            if spec in ("fs", "gs"):
                return self.finish(name, [spec])
            return self.finish(name, self.operands(spec), kind)
        raise _Invalid

    def _sel_prefix(self, op):
        return self.mandatory

    def _grp7(self):
        mod, reg, rm = self.read_modrm()
        if mod != 3:
            if reg == 5 and self.mandatory == 0xF3:
                return self.finish("rstorssp", [self.rm_operand(8)])
            names = ["sgdt", "sidt", "lgdt", "lidt", "smsw", None, "lmsw", "invlpg"]
            if names[reg] is None:
                raise _Invalid
            size = 10 if reg < 4 else (2 if reg in (4, 6) else 1)
            return self.finish(names[reg], [self.rm_operand(size if reg >= 4 else 0)])
        special = {(0, 1): "vmcall", (0, 2): "vmlaunch", (0, 3): "vmresume", (0, 4): "vmxoff",
                   (1, 0): "monitor", (1, 1): "mwait", (1, 2): "clac", (1, 3): "stac", (1, 7): "encls",
                   (2, 0): "xgetbv", (2, 1): "xsetbv", (2, 4): "vmfunc", (2, 5): "xend", (2, 6): "xtest",
                   (2, 7): "enclu", (3, 0): "vmrun", (3, 1): "vmmcall", (3, 2): "vmload", (3, 3): "vmsave",
                   (3, 4): "stgi", (3, 5): "clgi", (3, 6): "skinit", (3, 7): "invlpga",
                   (5, 6): "rdpkru" if False else "rdpkru", (5, 7): "wrpkru", (7, 0): "swapgs",
                   (7, 1): "rdtscp", (7, 2): "monitorx", (7, 3): "mwaitx", (7, 4): "clzero",
                   (7, 5): "rdpru"}
        if reg == 5 and rm == 6:
            return self.finish("rdpkru", [])
        if reg == 5 and rm == 7:
            return self.finish("wrpkru", [])
        if (reg, rm) in special:
            return self.finish(special[(reg, rm)], [])
        if reg == 4:
            return self.finish("smsw", [self.rm_operand(self.opsize())])
        if reg == 6:
            return self.finish("lmsw", [self.rm_operand(2)])
        if reg == 5:
            return self.finish("serialize" if rm == 0 else "grp7", [])
        raise _Invalid

    def _grp15(self):
        mod, reg, rm = self.read_modrm()
        if mod != 3:
            if self.mandatory == 0x66 and reg in (6, 7):
                return self.finish("clwb" if reg == 6 else "clflushopt", [self.rm_operand(1)])
            if self.mandatory == 0xF3 and reg == 6:
                return self.finish("clrssbsy", [self.rm_operand(8)])
            names = ["fxsave", "fxrstor", "ldmxcsr", "stmxcsr", "xsave", "xrstor", "xsaveopt", "clflush"]
            name = names[reg]
            if self.rex_w and reg in (0, 1, 4, 5, 6):
                name += "64"
            size = 4 if reg in (2, 3) else (1 if reg == 7 else 0)
            return self.finish(name, [self.rm_operand(size)])
        if self.rep == 0xF3 and reg < 4:
            name = ["rdfsbase", "rdgsbase", "wrfsbase", "wrgsbase"][reg]
            return self.finish(name, [self.reg_name(self.rm_field(), 8 if self.rex_w else 4)])
        if self.rep == 0xF3 and reg == 5:
            return self.finish("incsspq" if self.rex_w else "incsspd", [self.reg_name(self.rm_field(), 8 if self.rex_w else 4)])
        if reg == 6 and self.mandatory in (0xF3, 0xF2, 0x66):
            # WAITPKG: umonitor r64 / umwait r32 / tpause r32
            name = {0xF3: "umonitor", 0xF2: "umwait", 0x66: "tpause"}[self.mandatory]
            return self.finish(name, [self.reg_name(self.rm_field(), 8 if name == "umonitor" else 4)])
        names = {5: "lfence", 6: "mfence", 7: "sfence"}
        if reg in names:
            return self.finish(names[reg], [])
        raise _Invalid

    def _grp9(self):
        mod, reg, rm = self.read_modrm()
        if mod != 3:
            if reg == 1:
                return self.finish("cmpxchg16b" if self.rex_w else "cmpxchg8b",
                                   [self.rm_operand(16 if self.rex_w else 8)])
            names = {3: "xrstors", 4: "xsavec", 5: "xsaves", 6: "vmptrld", 7: "vmptrst"}
            if reg == 6 and self.mandatory == 0x66:
                return self.finish("vmclear", [self.rm_operand(8)])
            if reg == 6 and self.rep == 0xF3:
                return self.finish("vmxon", [self.rm_operand(8)])
            if reg in names:
                name = names[reg] + ("64" if self.rex_w and reg in (3, 4, 5) else "")
                return self.finish(name, [self.rm_operand(8 if reg >= 6 else 0)])
            raise _Invalid
        if reg == 6:
            return self.finish("rdrand", [self.reg_name(self.rm_field(), self.opsize())])
        if reg == 7:
            if self.rep == 0xF3:
                return self.finish("rdpid", [self.reg_name(self.rm_field(), 8)])
            return self.finish("rdseed", [self.reg_name(self.rm_field(), self.opsize())])
        raise _Invalid

    def _three_byte(self, esc, op):
        self.op = op
        table = _OP38 if esc == 0x38 else _OP3A
        entry = table.get(op, {}).get(self.mandatory)
        if entry is None:
            entry = table.get(op, {}).get(None)
        if entry is None:
            # unknown 0F38/0F3A opcode: still decode modrm (+imm8 for 0F3A) for the length
            self.read_modrm()
            if esc == 0x3A:
                self.ux(1)
            return self.finish(f"(0F{esc:02X}{op:02X}h)", [])
        name, spec = entry
        if esc == 0x3A and op in (0x16, 0x22) and self.rex_w:
            name = "pextrq" if op == 0x16 else "pinsrq"
        if name in ("wrss", "wruss"):
            name += "q" if self.rex_w else "d"
        if esc == 0x38 and op in (0xF0, 0xF1) and self.rep == 0xF2:
            name = "crc32"
            spec = "Gy,Eb" if op == 0xF0 else "Gy,Ev"
        return self.finish(name, self.operands(spec))

    # -- VEX / EVEX -- #
    def _vex(self, b):
        if b == 0xC5:
            p1 = self.u8()
            r = 0 if p1 & 0x80 else 4
            self.rex = 0x40 | r
            vvvv = (~p1 >> 3) & 0xF
            L = (p1 >> 2) & 1
            pp = p1 & 3
            mmmmm = 1
            W = 0
        elif b == 0xC4:
            p1 = self.u8()
            p2 = self.u8()
            self.rex = 0x40 | (0 if p1 & 0x80 else 4) | (0 if p1 & 0x40 else 2) | (0 if p1 & 0x20 else 1)
            mmmmm = p1 & 0x1F
            W = p2 >> 7
            if W:
                self.rex |= 8
            vvvv = (~p2 >> 3) & 0xF
            L = (p2 >> 2) & 1
            pp = p2 & 3
        else:   # EVEX
            p0, p1, p2 = self.u8(), self.u8(), self.u8()
            self.rex = 0x40 | (0 if p0 & 0x80 else 4) | (0 if p0 & 0x40 else 2) | (0 if p0 & 0x20 else 1)
            mmmmm = p0 & 3
            W = p1 >> 7
            if W:
                self.rex |= 8
            vvvv = (~p1 >> 3) & 0xF
            pp = p1 & 3
            L = (p2 >> 5) & 3          # 0 xmm, 1 ymm, 2 zmm
        self.vex = {"vvvv": vvvv, "L": L, "pp": pp, "map": mmmmm, "W": W, "evex": b == 0x62}
        self.mandatory = {0: None, 1: 0x66, 2: 0xF3, 3: 0xF2}[pp]
        self.rep = None
        op = self.u8()
        self.op = op
        if mmmmm not in (1, 2, 3):
            raise _Invalid
        if (mmmmm, op) in _VEXONLY and pp in _VEXONLY[(mmmmm, op)]:
            name, spec = _VEXONLY[(mmmmm, op)][pp]
            if op == 0x77 and mmmmm == 1:
                return self.finish("vzeroall" if L else "vzeroupper", [])
            if name.startswith("v") and W and name.endswith("ps") and "fm" in name:
                name = name[:-2] + "pd"
            if name.startswith("v") and W and name.endswith("ss") and "fm" in name:
                name = name[:-2] + "sd"
            return self.finish(name, self.operands(spec))
        table = {1: _OP2P, 2: _OP38, 3: _OP3A}[mmmmm]
        entry = table.get(op, {}).get(self.mandatory)
        if mmmmm == 1 and op in _SHIFTGRP and self.mandatory == 0x66:
            self.read_modrm()
            name = _SHIFTGRP[op].get(self.modrm[1])
            if name is None:
                raise _Invalid
            reg = "ymm" if L else "xmm"
            return self.finish("v" + name, [f"{reg}{vvvv}", f"{reg}{self.rm_field()}", self.operand("Ib")])
        if entry is None:
            # unknown VEX/EVEX opcode: generic name, decode modrm (+imm8) for the length
            self.read_modrm()
            if mmmmm == 3 or (mmmmm == 1 and op in (0x70, 0x71, 0x72, 0x73, 0xC2, 0xC4, 0xC5, 0xC6)):
                self.ux(1)
            pref = "evex" if b == 0x62 else "vex"
            return self.finish(f"({pref}.{mmmmm}.{op:02X}h)", [])
        name, spec = entry
        name = "v" + name
        if mmmmm == 1 and op in (0x6E, 0x7E) and W:
            name = "vmovq"
        # 3-operand AVX forms: insert VEX.vvvv as the first source
        toks = spec.split(",") if spec else []
        if len(toks) >= 2 and toks[0][0] in "VP" and toks[1][0] in "WUQE" and \
                name not in ("vmovups", "vmovupd", "vmovaps", "vmovapd", "vmovdqa", "vmovdqu", "vmovd",
                             "vmovq", "vpshufd", "vpshufhw", "vpshuflw", "vmovsldup", "vmovshdup",
                             "vmovddup", "vucomiss", "vucomisd", "vcomiss", "vcomisd", "vcvtdq2ps",
                             "vcvtps2dq", "vcvttps2dq", "vcvtps2pd", "vcvtpd2ps", "vsqrtps", "vsqrtpd",
                             "vrsqrtps", "vrcpps", "vptest", "vlddqu", "vmovntdqa", "vpabsb", "vpabsw",
                             "vpabsd", "vaesimc", "vphminposuw") \
                and not name.startswith(("vpmovsx", "vpmovzx", "vcvt", "vround")) or \
                (name in ("vmovss", "vmovsd") and self.modrm_is_reg()):
            if name.startswith(("vround",)) and op in (0x08, 0x09):
                pass
            else:
                toks.insert(1, "Hx")
        if mmmmm == 3 and toks and toks[-1] != "Ib":
            toks.append("Ib")
        return self.finish(name, self.operands(",".join(toks)))

    def modrm_is_reg(self):
        if self.modrm is None:
            self.read_modrm()
        return self.modrm[0] == 3


# --------------------------------------------------------------------------- #
#  PDB reader (MSF 7.0) and symbol store: publics, types, source lines, locals
# --------------------------------------------------------------------------- #

_MSF_MAGIC = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\x00\x00\x00"

# CodeView register numbers (AMD64) -> name
_CV_REG = {17: "eax", 18: "ecx", 19: "edx", 20: "ebx", 21: "esp", 22: "ebp", 23: "esi", 24: "edi",
           328: "rax", 329: "rbx", 330: "rcx", 331: "rdx", 332: "rsi", 333: "rdi", 334: "rbp",
           335: "rsp", 336: "r8", 337: "r9", 338: "r10", 339: "r11", 340: "r12", 341: "r13",
           342: "r14", 343: "r15", 33: "rip"}
for _i in range(8):
    _CV_REG[360 + _i] = f"r{8 + _i}d"
_CV_REG_64 = {"eax": "rax", "ecx": "rcx", "edx": "rdx", "ebx": "rbx", "esp": "rsp", "ebp": "rbp",
              "esi": "rsi", "edi": "rdi", **{f"r{n}d": f"r{n}" for n in range(8, 16)}}

# Primitive types: low byte -> (name, size)
_PRIM = {0x03: ("void", 0), 0x08: ("HRESULT", 4), 0x10: ("char", 1), 0x20: ("unsigned char", 1),
         0x68: ("char", 1), 0x69: ("unsigned char", 1), 0x70: ("char", 1), 0x71: ("wchar_t", 2),
         0x7A: ("char16_t", 2), 0x7B: ("char32_t", 4), 0x7C: ("char8_t", 1),
         0x11: ("short", 2), 0x21: ("unsigned short", 2), 0x72: ("short", 2), 0x73: ("unsigned short", 2),
         0x12: ("long", 4), 0x22: ("unsigned long", 4), 0x74: ("int", 4), 0x75: ("unsigned int", 4),
         0x13: ("__int64", 8), 0x23: ("unsigned __int64", 8), 0x76: ("__int64", 8),
         0x77: ("unsigned __int64", 8), 0x14: ("__int128", 16), 0x24: ("unsigned __int128", 16),
         0x40: ("float", 4), 0x41: ("double", 8), 0x42: ("long double", 10), 0x30: ("bool", 1),
         0x31: ("bool", 2), 0x32: ("bool", 4), 0x33: ("bool", 8)}


def _cstr(data, off):
    end = data.index(b"\0", off)
    return data[off:end].decode("utf-8", "replace"), end + 1


def _leaf(data, off):
    """CodeView numeric leaf -> (value, next offset)."""
    v = struct.unpack_from("<H", data, off)[0]
    off += 2
    if v < 0x8000:
        return v, off
    fmt = {0x8000: "<b", 0x8001: "<h", 0x8002: "<H", 0x8003: "<i", 0x8004: "<I",
           0x8009: "<q", 0x800A: "<Q"}.get(v)
    if fmt is None:
        return 0, off
    n = struct.calcsize(fmt)
    return struct.unpack_from(fmt, data, off)[0], off + n


class PdbError(Exception):
    pass


class Msf:
    """Multi-Stream File container of a PDB."""

    def __init__(self, data: bytes):
        if not data.startswith(_MSF_MAGIC):
            raise PdbError("not an MSF 7.0 PDB")
        self.d = data
        bs, _fpm, nblocks, dir_bytes, _u, bmap = struct.unpack_from("<6I", data, 32)
        self.bs = bs
        nblk = (dir_bytes + bs - 1) // bs
        dir_blocks = struct.unpack_from(f"<{nblk}I", data, bmap * bs)
        directory = b"".join(data[b * bs:(b + 1) * bs] for b in dir_blocks)[:dir_bytes]
        n = struct.unpack_from("<I", directory, 0)[0]
        sizes = struct.unpack_from(f"<{n}I", directory, 4)
        off = 4 + 4 * n
        self.streams = []
        for size in sizes:
            if size == 0xFFFFFFFF:
                self.streams.append(None)
                continue
            cnt = (size + bs - 1) // bs
            blocks = struct.unpack_from(f"<{cnt}I", directory, off)
            off += 4 * cnt
            self.streams.append((size, blocks))
        self._cache = {}

    def stream(self, i: int) -> bytes:
        if i >= len(self.streams) or self.streams[i] is None or i == 0xFFFF:
            return b""
        if i not in self._cache:
            size, blocks = self.streams[i]
            bs = self.bs
            self._cache[i] = b"".join(self.d[b * bs:(b + 1) * bs] for b in blocks)[:size]
        return self._cache[i]


class Pdb:
    """Reads what a debugger needs from a PDB: identity, publics/procedures (address
    <-> name), struct/union layouts for field offsets, and, in private PDBs, source
    lines and the local variables / parameters of each function."""

    def __init__(self, path):
        self.path = str(path)
        self.msf = Msf(Path(path).read_bytes())
        info = self.msf.stream(1)
        self.version, self.signature, self.age = struct.unpack_from("<III", info, 0)
        self.guid = info[12:28]
        self.named = self._named_streams(info, 28)
        self._dbi()
        self._types = None
        self._symbols = None      # sorted [(rva, size or 0, name)]
        self._by_name = None
        self._modules_parsed = False
        self._procs = []          # private PDBs: [(rva, size, name, module index, symbol offset)]
        self._lines = None        # [(rva, length, file, line)] sorted

    # -- identity -- #
    @property
    def guid_age(self) -> str:
        d1, d2, d3 = struct.unpack_from("<IHH", self.guid, 0)
        return f"{d1:08X}{d2:04X}{d3:04X}{self.guid[8:].hex().upper()}{self.dbi_age:X}"

    def _named_streams(self, info, off):
        size = struct.unpack_from("<I", info, off)[0]
        strings = info[off + 4: off + 4 + size]
        off += 4 + size
        _count, _cap = struct.unpack_from("<II", info, off)
        off += 8
        nwords = struct.unpack_from("<I", info, off)[0]
        present = struct.unpack_from(f"<{nwords}I", info, off + 4)
        off += 4 + 4 * nwords
        dwords = struct.unpack_from("<I", info, off)[0]
        off += 4 + 4 * dwords
        out = {}
        for w, word in enumerate(present):
            for bit in range(32):
                if word >> bit & 1:
                    key, val = struct.unpack_from("<II", info, off)
                    off += 8
                    name = strings[key:strings.index(b"\0", key)].decode("utf-8", "replace")
                    out[name] = val
        return out

    # -- DBI stream -- #
    def _dbi(self):
        d = self.msf.stream(3)
        if len(d) < 64:
            raise PdbError("PDB without DBI stream")
        (_vsig, _vhdr, self.dbi_age, self.gsi_stream, _build, self.psi_stream, _dllver,
         self.symrec_stream, _rbld, modsz, secconsz, secmapsz, srcsz, tsmapsz, _mfc,
         dbgsz, ecsz, _flags, self.machine) = struct.unpack_from("<iIIHHHHHHiiiiiIiiHH", d, 0)
        off = 64
        self.modules = []
        end = off + modsz
        while off < end:
            (sym_stream,) = struct.unpack_from("<H", d, off + 34)
            symsz, c11, c13 = struct.unpack_from("<III", d, off + 36)
            name, o2 = _cstr(d, off + 64)
            obj, o3 = _cstr(d, o2)
            self.modules.append({"name": name, "obj": obj, "stream": sym_stream,
                                 "symsz": symsz, "c11": c11, "c13": c13})
            off = (o3 + 3) & ~3
        dbg_off = 64 + modsz + secconsz + secmapsz + srcsz + tsmapsz + ecsz
        dbg = struct.unpack_from(f"<{dbgsz // 2}H", d, dbg_off) if dbgsz else ()
        self.sections = []
        if len(dbg) > 5 and dbg[5] != 0xFFFF:
            sh = self.msf.stream(dbg[5])
            for i in range(len(sh) // 40):
                self.sections.append(struct.unpack_from("<I", sh, i * 40 + 12)[0])
        self.omap = len(dbg) > 4 and dbg[4] != 0xFFFF and len(self.msf.stream(dbg[4])) > 0

    def rva(self, segment: int, offset: int):
        if 1 <= segment <= len(self.sections):
            return self.sections[segment - 1] + offset
        return None

    # -- symbols: publics + global data + procedures -- #
    def _load_symbols(self):
        if self._symbols is not None:
            return
        rec = self.msf.stream(self.symrec_stream)
        syms = {}
        off = 0
        n = len(rec)
        while off + 4 <= n:
            reclen, kind = struct.unpack_from("<HH", rec, off)
            body = off + 4
            if kind == 0x110E:                         # S_PUB32
                _fl, o, seg = struct.unpack_from("<IIH", rec, body)
                name, _ = _cstr(rec, body + 10)
                r = self.rva(seg, o)
                if r is not None:
                    syms.setdefault(name, [r, 0])
            elif kind in (0x110C, 0x110D):             # S_LDATA32 / S_GDATA32
                _ti, o, seg = struct.unpack_from("<IIH", rec, body)
                name, _ = _cstr(rec, body + 10)
                r = self.rva(seg, o)
                if r is not None:
                    syms.setdefault(name, [r, 0])
            off += 2 + reclen
        self._parse_modules()
        for rva, size, name, _m, _o in self._procs:
            ent = syms.get(name)
            if ent is None or ent[0] == rva:
                syms[name] = [rva, size]
            else:
                syms[f"{name}"] = [rva, size]
        self._by_name = {k: v[0] for k, v in syms.items()}
        self._symbols = sorted((v[0], v[1], k) for k, v in syms.items())
        self._sym_rvas = [s[0] for s in self._symbols]

    def address_of(self, name: str):
        self._load_symbols()
        return self._by_name.get(name)

    def symbol_at(self, rva: int):
        """-> (name, displacement) of the closest symbol at or below rva, or None."""
        self._load_symbols()
        i = bisect.bisect_right(self._sym_rvas, rva) - 1
        while i >= 0:
            srva, size, name = self._symbols[i]
            if size and rva >= srva + size:
                # inside the gap after a sized function: keep looking for a covering symbol
                if i > 0 and self._symbols[i - 1][0] == srva:
                    i -= 1
                    continue
                return name, rva - srva
            return name, rva - srva
        return None

    # -- module streams: procedures, locals, lines -- #
    def _parse_modules(self):
        if self._modules_parsed:
            return
        self._modules_parsed = True
        for mi, m in enumerate(self.modules):
            if m["stream"] == 0xFFFF or not m["symsz"]:
                continue
            data = self.msf.stream(m["stream"])
            off, end = 4, m["symsz"]
            while off + 4 <= end:
                reclen, kind = struct.unpack_from("<HH", data, off)
                if kind in (0x1110, 0x110F, 0x1147, 0x1146):   # S_[GL]PROC32[_ID]
                    size = struct.unpack_from("<I", data, off + 16)[0]
                    co, seg = struct.unpack_from("<IH", data, off + 32)
                    name, _ = _cstr(data, off + 39)
                    r = self.rva(seg, co)
                    if r is not None:
                        self._procs.append((r, size, name, mi, off))
                off += 2 + reclen
        self._procs.sort()
        self._proc_rvas = [p[0] for p in self._procs]

    def procedure_at(self, rva: int):
        self._parse_modules()
        i = bisect.bisect_right(self._proc_rvas, rva) - 1
        if i >= 0:
            p = self._procs[i]
            if p[0] <= rva < p[0] + max(p[1], 1):
                return p
        return None

    @property
    def has_private_info(self) -> bool:
        self._parse_modules()
        return bool(self._procs)

    def _names(self):
        if not hasattr(self, "_names_buf"):
            s = self.msf.stream(self.named.get("/names", 0xFFFF)) if "/names" in self.named else b""
            self._names_buf = s[12:12 + struct.unpack_from("<I", s, 8)[0]] if len(s) >= 12 else b""
        return self._names_buf

    def _load_lines(self):
        if self._lines is not None:
            return
        self._lines = []
        names = self._names()
        for m in self.modules:
            if m["stream"] == 0xFFFF or not m["c13"]:
                continue
            data = self.msf.stream(m["stream"])
            off = m["symsz"] + m["c11"]
            end = off + m["c13"]
            checks = {}
            blocks = []
            while off + 8 <= end:
                kind, ln = struct.unpack_from("<II", data, off)
                body = off + 8
                if kind == 0xF4:            # DEBUG_S_FILECHKSMS
                    p = body
                    while p + 6 <= body + ln:
                        name_off, cb, _t = struct.unpack_from("<IBB", data, p)
                        checks[p - body] = name_off
                        p = (p + 6 + cb + 3) & ~3
                elif kind == 0xF2:          # DEBUG_S_LINES
                    con_off, seg, flags, cb = struct.unpack_from("<IHHI", data, body)
                    base = self.rva(seg, con_off)
                    p = body + 12
                    while p + 12 <= body + ln and base is not None:
                        file_id, nlines, cbblock = struct.unpack_from("<III", data, p)
                        entries = []
                        for k in range(nlines):
                            lo, lf = struct.unpack_from("<II", data, p + 12 + 8 * k)
                            entries.append((base + lo, lf & 0xFFFFFF))
                        blocks.append((file_id, entries, base + cb))
                        p += cbblock
                off = (body + ln + 3) & ~3
            for file_id, entries, con_end in blocks:
                noff = checks.get(file_id)
                fname = names[noff:names.index(b"\0", noff)].decode("utf-8", "replace") if noff is not None else "?"
                for k, (r, line) in enumerate(entries):
                    nxt = entries[k + 1][0] if k + 1 < len(entries) else con_end
                    self._lines.append((r, max(nxt - r, 0), fname, line))
        self._lines.sort()
        self._line_rvas = [x[0] for x in self._lines]

    def line_at(self, rva: int):
        """-> (file, line) for a code address, or None (public PDBs have no lines)."""
        self._load_lines()
        i = bisect.bisect_right(self._line_rvas, rva) - 1
        if i >= 0:
            r, ln, fname, line = self._lines[i]
            if rva < r + max(ln, 1):
                return fname, line
        return None

    def locals_at(self, rva: int):
        """Locals and parameters of the function at rva (private PDB): list of dicts
        {name, type, param, where}; where is ("regrel", reg, offset), ("reg", reg) or
        ("frame", offset) relative to the function's local base pointer."""
        p = self.procedure_at(rva)
        if not p:
            return None, []
        _r, _size, fname, mi, sym_off = p
        data = self.msf.stream(self.modules[mi]["stream"])
        reclen, kind = struct.unpack_from("<HH", data, sym_off)
        proc_end = struct.unpack_from("<I", data, sym_off + 8)[0]
        nparams = 0
        if kind in (0x1110, 0x110F):                 # S_GPROC32 / S_LPROC32: TPI function type
            fti = struct.unpack_from("<I", data, sym_off + 28)[0]
            try:
                if fti >= 0x1000:
                    fk, fb, _fe = self._rec(fti)
                    if fk == 0x1008:                 # LF_PROCEDURE: parameter count
                        nparams = struct.unpack_from("<H", self._tpi, fb + 6)[0]
                    elif fk == 0x1009:               # LF_MFUNCTION
                        nparams = struct.unpack_from("<H", self._tpi, fb + 14)[0]
            except (IndexError, struct.error):
                nparams = 0
        off = sym_off + 2 + reclen
        out = []
        frame_base = "rsp"
        pending = None
        depth = 0
        while off < proc_end and off + 4 <= len(data):
            reclen, kind = struct.unpack_from("<HH", data, off)
            body = off + 4
            if kind == 0x1012:                      # S_FRAMEPROC
                flags = struct.unpack_from("<I", data, body + 26)[0]
                frame_base = {1: "rsp", 2: "rbp", 3: "r13"}.get((flags >> 14) & 3, "rsp")
            elif kind == 0x1111:                    # S_REGREL32 (/Od: parameters come first)
                o, ti, reg = struct.unpack_from("<iIH", data, body)
                name, _ = _cstr(data, body + 10)
                regrel_seen = sum(1 for v in out if v.get("regrel"))
                out.append({"name": name, "type": ti, "param": regrel_seen < nparams and depth == 0,
                            "where": ("regrel", _CV_REG.get(reg, f"cv{reg}"), o), "regrel": True})
            elif kind == 0x113E:                    # S_LOCAL (+ following S_DEFRANGE_*)
                ti, fl = struct.unpack_from("<IH", data, body)
                name, _ = _cstr(data, body + 6)
                pending = {"name": name, "type": ti, "param": bool(fl & 1), "where": None}
                out.append(pending)
            elif kind in (0x1142, 0x1144) and pending is not None:   # FRAMEPOINTER_REL[_FULL_SCOPE]
                if pending["where"] is None:
                    pending["where"] = ("frame", struct.unpack_from("<i", data, body)[0])
            elif kind == 0x1145 and pending is not None:             # DEFRANGE_REGISTER_REL
                reg, _f, o = struct.unpack_from("<HHi", data, body)
                if pending["where"] is None:
                    pending["where"] = ("regrel", _CV_REG.get(reg, f"cv{reg}"), o)
            elif kind == 0x1141 and pending is not None:             # DEFRANGE_REGISTER
                reg = struct.unpack_from("<H", data, body)[0]
                if pending["where"] is None:
                    pending["where"] = ("reg", _CV_REG.get(reg, f"cv{reg}"))
            elif kind in (0x1103, 0x114D):          # S_BLOCK32 / S_INLINESITE: nested scope
                depth += 1
            elif kind in (0x0006, 0x114E):
                depth -= 1
            off += 2 + reclen
        for v in out:
            if v["where"] and v["where"][0] == "frame":
                v["where"] = ("regrel", frame_base, v["where"][1])
        return fname, out

    # -- types (TPI) -- #
    def _load_types(self):
        if self._types is not None:
            return
        d = self.msf.stream(2)
        _ver, hdr, ti_begin, ti_end, rec_bytes = struct.unpack_from("<IIIII", d, 0)
        self._tpi = d
        self._ti_begin = ti_begin
        offs = []
        off = hdr
        end = hdr + rec_bytes
        while off < end:
            offs.append(off)
            off += 2 + struct.unpack_from("<H", d, off)[0]
        self._types = offs
        self._udt = {}
        for i, o in enumerate(offs):
            kind = struct.unpack_from("<H", d, o + 2)[0]
            if kind in (0x1504, 0x1505, 0x1506, 0x1507):
                prop = struct.unpack_from("<H", d, o + 6)[0]
                if prop & 0x80:            # forward reference
                    continue
                name = self._udt_name(o, kind)
                prev = self._udt.get(name)
                # a name can have several definitions (e.g. a reduced _KPRCB next to the
                # real one): keep the largest
                if prev is None or self._raw_size(o, kind) > self._raw_size(self._types[prev - ti_begin], None):
                    self._udt[name] = ti_begin + i

    def _raw_size(self, o, kind):
        d = self._tpi
        if kind is None:
            kind = struct.unpack_from("<H", d, o + 2)[0]
        if kind in (0x1504, 0x1505):
            return _leaf(d, o + 4 + 16)[0]
        if kind == 0x1506:
            return _leaf(d, o + 4 + 8)[0]
        return 0

    def _rec(self, ti):
        self._load_types()
        o = self._types[ti - self._ti_begin]
        ln, kind = struct.unpack_from("<HH", self._tpi, o)
        return kind, o + 4, o + 2 + ln

    def _udt_name(self, o, kind):
        d = self._tpi
        if kind in (0x1504, 0x1505):
            _sz, p = _leaf(d, o + 4 + 16)
        elif kind == 0x1506:
            _sz, p = _leaf(d, o + 4 + 8)
        else:
            p = o + 4 + 12
        return _cstr(d, p)[0]

    def find_type(self, name: str):
        self._load_types()
        return self._udt.get(name)

    def _resolve_fwd(self, ti):
        if ti < 0x1000:
            return ti
        kind, body, _end = self._rec(ti)
        if kind in (0x1504, 0x1505, 0x1506, 0x1507):
            prop = struct.unpack_from("<H", self._tpi, body + 2)[0]
            if prop & 0x80:
                full = self._udt.get(self._udt_name(body - 4, kind))
                if full:
                    return full
        return ti

    def type_size(self, ti) -> int:
        ti = self._resolve_fwd(ti)
        if ti < 0x1000:
            mode = (ti >> 8) & 7
            if mode:
                return 8 if mode == 6 else 4
            return _PRIM.get(ti & 0xFF, ("?", 0))[1]
        kind, b, _e = self._rec(ti)
        d = self._tpi
        if kind in (0x1504, 0x1505):
            return _leaf(d, b + 16)[0]
        if kind == 0x1506:
            return _leaf(d, b + 8)[0]
        if kind == 0x1507:
            return self.type_size(struct.unpack_from("<I", d, b + 4)[0])
        if kind == 0x1002:
            return (struct.unpack_from("<I", d, b + 4)[0] >> 13) & 0x3F or 8
        if kind == 0x1001:
            return self.type_size(struct.unpack_from("<I", d, b)[0])
        if kind == 0x1503:
            return _leaf(d, b + 8)[0]
        if kind == 0x1205:
            return self.type_size(struct.unpack_from("<I", d, b)[0])
        return 0

    def type_name(self, ti) -> str:
        ti = self._resolve_fwd(ti)
        if ti < 0x1000:
            base = _PRIM.get(ti & 0xFF, (f"T{ti & 0xFF:#x}", 0))[0]
            return base + (" *" if (ti >> 8) & 7 else "")
        kind, b, _e = self._rec(ti)
        d = self._tpi
        if kind in (0x1504, 0x1505, 0x1506, 0x1507):
            return self._udt_name(b - 4, kind)
        if kind == 0x1002:
            return self.type_name(struct.unpack_from("<I", d, b)[0]) + " *"
        if kind == 0x1001:
            mods = struct.unpack_from("<H", d, b + 4)[0]
            q = ("const " if mods & 1 else "") + ("volatile " if mods & 2 else "")
            return q + self.type_name(struct.unpack_from("<I", d, b)[0])
        if kind == 0x1503:
            el = struct.unpack_from("<I", d, b)[0]
            total = _leaf(d, b + 8)[0]
            es = self.type_size(el) or 1
            return f"{self.type_name(el)} [{total // es}]"
        if kind == 0x1205:
            return self.type_name(struct.unpack_from("<I", d, b)[0])
        if kind in (0x1008, 0x1009):
            return "<function>"
        return f"<type {ti:#x}>"

    def type_kind(self, ti):
        ti = self._resolve_fwd(ti)
        if ti < 0x1000:
            return "pointer" if (ti >> 8) & 7 else "base"
        kind = self._rec(ti)[0]
        return {0x1002: "pointer", 0x1001: "modifier", 0x1503: "array", 0x1504: "struct",
                0x1505: "struct", 0x1506: "union", 0x1507: "enum", 0x1205: "bitfield"}.get(kind, "other")

    def fields(self, ti):
        """Members of a struct/union: [(name, offset, type, bitpos, bitlen)]."""
        ti = self._resolve_fwd(ti)
        if ti < 0x1000:
            return []
        kind, b, _e = self._rec(ti)
        d = self._tpi
        if kind in (0x1504, 0x1505, 0x1506):
            fl = struct.unpack_from("<I", d, b + 4)[0]
        else:
            return []
        out = []
        while fl:
            fkind, p, end = self._rec(fl)
            fl = 0
            while p + 2 <= end:
                if d[p] >= 0xF0:                         # LF_PAD1..15: skip the padding bytes
                    p += d[p] & 0x0F or 1
                    continue
                sub = struct.unpack_from("<H", d, p)[0]
                if sub == 0x150D:                        # LF_MEMBER
                    _attr, mti = struct.unpack_from("<HI", d, p + 2)
                    moff, q = _leaf(d, p + 8)
                    name, q = _cstr(d, q)
                    bitpos = bitlen = None
                    if mti >= 0x1000:
                        mk, mb, _me = self._rec(mti)
                        if mk == 0x1205:
                            bt, bitlen, bitpos = struct.unpack_from("<IBB", d, mb)
                            mti = bt
                    out.append((name, moff, mti, bitpos, bitlen))
                    p = q
                elif sub == 0x1400:                      # LF_BCLASS
                    _attr, bti = struct.unpack_from("<HI", d, p + 2)
                    boff, q = _leaf(d, p + 8)
                    out.append(("<base>", boff, bti, None, None))
                    p = q
                elif sub in (0x1401, 0x1402):            # LF_VBCLASS / LF_IVBCLASS
                    _x, q = _leaf(d, p + 12)
                    _y, q = _leaf(d, q)
                    p = q
                elif sub == 0x1502:                      # LF_ENUMERATE
                    _v, q = _leaf(d, p + 4)
                    _n, q = _cstr(d, q)
                    p = q
                elif sub == 0x1510:                      # LF_NESTTYPE
                    _n, q = _cstr(d, p + 8)
                    p = q
                elif sub == 0x150E:                      # LF_STMEMBER
                    _n, q = _cstr(d, p + 8)
                    p = q
                elif sub == 0x150F:                      # LF_METHOD
                    _n, q = _cstr(d, p + 8)
                    p = q
                elif sub == 0x1511:                      # LF_ONEMETHOD
                    attr = struct.unpack_from("<H", d, p + 2)[0]
                    q = p + 8 + (4 if ((attr >> 2) & 7) in (4, 6) else 0)
                    _n, q = _cstr(d, q)
                    p = q
                elif sub == 0x1409:                      # LF_VFUNCTAB
                    p += 8
                elif sub == 0x1404:                      # LF_INDEX: continuation
                    fl = struct.unpack_from("<I", d, p + 4)[0]
                    p += 8
                else:
                    break
        return out

    def field_offset(self, type_name: str, path: str):
        """Offset and type of 'Field' or 'Field.SubField', looking inside anonymous
        unions/structs as C does. -> (offset, type index) or None."""
        ti = self.find_type(type_name)
        if ti is None:
            return None
        off = 0
        for part in path.split("."):
            found = self._find_member(ti, part)
            if found is None:
                return None
            moff, mti = found
            off += moff
            ti = self._resolve_fwd(mti)
        return off, ti

    def _find_member(self, ti, name, depth=0):
        if depth > 8:
            return None
        members = self.fields(ti)
        for mname, moff, mti, _bp, _bl in members:
            if mname == name:
                return moff, mti
        for mname, moff, mti, _bp, _bl in members:
            if mname.startswith("<") or mname.startswith("__") or mname == "<base>":
                sub = self._find_member(self._resolve_fwd(mti), name, depth + 1)
                if sub is not None:
                    return moff + sub[0], sub[1]
            elif mti >= 0x1000 and self.type_name(mti).startswith(("<unnamed", "<anonymous", "__unnamed")):
                sub = self._find_member(self._resolve_fwd(mti), name, depth + 1)
                if sub is not None:
                    return moff + sub[0], sub[1]
        return None


# --------------------------------------------------------------------------- #
#  Symbol store: finds or downloads the PDB of a module (CodeView RSDS record)
# --------------------------------------------------------------------------- #
MS_SYMBOL_SERVER = "https://msdl.microsoft.com/download/symbols"


def rsds_from_image(read, base: int):
    """CodeView record of a PE image mapped at base: (pdb name, GUID+age key, pdb path).
    read(addr, size) -> bytes."""
    mz = read(base, 0x40)
    if mz[:2] != b"MZ":
        return None
    pe = base + struct.unpack_from("<I", mz, 0x3C)[0]
    nt = read(pe, 0x108)
    if nt[:4] != b"PE\0\0":
        return None
    magic = struct.unpack_from("<H", nt, 24)[0]
    ddir = 24 + (112 if magic == 0x20B else 96)
    dbg_rva, dbg_size = struct.unpack_from("<II", nt, ddir + 6 * 8)
    if not dbg_rva or not dbg_size:
        return None
    dirs = read(base + dbg_rva, min(dbg_size, 28 * 16))
    for i in range(len(dirs) // 28):
        (_c, _t, _maj, _min, typ, size, rva, _ptr) = struct.unpack_from("<IIHHIIII", dirs, i * 28)
        if typ == 2 and size >= 24:
            cv = read(base + rva, min(size, 600))
            if cv[:4] != b"RSDS":
                continue
            g = cv[4:20]
            age = struct.unpack_from("<I", cv, 20)[0]
            path = cv[24:cv.index(b"\0", 24)].decode("utf-8", "replace") if b"\0" in cv[24:] else ""
            d1, d2, d3 = struct.unpack_from("<IHH", g, 0)
            key = f"{d1:08X}{d2:04X}{d3:04X}{g[8:].hex().upper()}{age:X}"
            name = path.replace("/", "\\").rsplit("\\", 1)[-1] or "unknown.pdb"
            return name, key, path
    return None


class PeFile:
    """A PE image file (e.g. downloaded from the symbol server): read by RVA."""

    def __init__(self, data: bytes):
        if data[:2] != b"MZ":
            raise PdbError("pas une image PE")
        self.d = data
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        nsec = struct.unpack_from("<H", data, pe + 6)[0]
        opt = struct.unpack_from("<H", data, pe + 20)[0]
        sec = pe + 24 + opt
        self.sections = []
        for i in range(nsec):
            vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", data, sec + i * 40 + 8)
            self.sections.append((va, max(vsize, rawsize), rawptr, rawsize))
        self.header_size = sec + nsec * 40
        self.timestamp = struct.unpack_from("<I", data, pe + 8)[0]
        self.size_of_image = struct.unpack_from("<I", data, pe + 24 + 56)[0]
        self.checksum = struct.unpack_from("<I", data, pe + 24 + 64)[0]

    def read(self, rva, size):
        if rva + size <= self.header_size:
            return self.d[rva:rva + size]
        for va, vsize, rawptr, rawsize in self.sections:
            if va <= rva < va + vsize:
                o = rva - va
                chunk = self.d[rawptr + o: rawptr + min(o + size, rawsize)]
                return chunk + b"\0" * (min(size, vsize - o) - len(chunk))
        return b""


class SymbolStore:
    """Local search path (private PDBs) + cache + Microsoft public symbol server."""

    def __init__(self, sympath=None, cache=None, download=True, log=None):
        self.paths = [p for p in (sympath or []) if p]
        env = os.environ.get("WINEYE_SYMPATH") or os.environ.get("_NT_SYMBOL_PATH_LOCAL")
        if env:
            self.paths += [p for p in env.split(";") if p]
        self.cache = Path(cache or (Path.home() / ".wineye" / "symbols"))
        self.download = download
        self.log = log or (lambda msg: None)
        self._pdbs = {}

    def find(self, name: str, key: str, orig_path: str = ""):
        cand = []
        for root in self.paths:
            r = Path(root)
            cand += [r / name / key / name, r / name]
        if orig_path:
            cand.append(Path(orig_path))
        cand.append(self.cache / name / key / name)
        for c in cand:
            try:
                if c.is_file():
                    pdb = Pdb(c)
                    if pdb.guid_age == key:
                        return pdb
            except (OSError, PdbError, struct.error):
                continue
        if self.download:
            return self._download(name, key)
        return None

    def _download(self, name, key):
        dest = self.cache / name / key / name
        url = f"{MS_SYMBOL_SERVER}/{name}/{key}/{name}"
        self.log(f"[*] symboles : téléchargement {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "Microsoft-Symbol-Server/10.0.0.0"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
        except Exception as e:
            self.log(f"[~] symboles indisponibles pour {name} ({e})")
            return None
        if not data.startswith(_MSF_MAGIC):
            self.log(f"[~] {name} : réponse du serveur de symboles inattendue")
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return Pdb(dest)

    def image(self, name: str, timestamp: int, size: int, checksum: int = 0, file_size: int = 0,
              path: str = ""):
        """Image file of a module (name, TimeDateStamp, SizeOfImage): local cache, then the
        symbol server - used when the PE header of a module is not in the dump. Without a
        TimeDateStamp, a local copy (search path, cache, or the same path on this machine)
        is used when its SizeOfImage, CheckSum and file size all match."""
        if not timestamp:
            return self._local_image(name, size, checksum, file_size, path)
        key = f"{timestamp:08X}{size:X}"
        cached = self.cache / name / key / name
        for root in [Path(r) for r in self.paths] + [self.cache]:
            f = root / name / key / name
            if f.is_file():
                try:
                    return PeFile(f.read_bytes())
                except PdbError:
                    pass
        if not self.download or not size:
            return self._local_image(name, size, checksum, file_size, path)
        url = f"{MS_SYMBOL_SERVER}/{name}/{key}/{name}"
        self.log(f"[*] symboles : téléchargement de l'image {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "Microsoft-Symbol-Server/10.0.0.0"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
        except Exception as e:
            self.log(f"[~] image indisponible pour {name} ({e})")
            return None
        if data[:2] != b"MZ":
            return None
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
        return PeFile(data)

    def _local_image(self, name, size, checksum, file_size, path=""):
        if not size or not (checksum or file_size):
            return None              # SizeOfImage alone would be too weak an identification
        cand = []
        for root in [Path(r) for r in self.paths] + [self.cache]:
            cand.append(root / name)
            if (root / name).is_dir():
                cand += sorted((root / name).glob(f"*/{name}"))
        low = path.lower()
        for prefix in ("\\windows\\", "\\systemroot\\"):
            if low.startswith(prefix):
                windir = os.environ.get("SystemRoot") or os.environ.get("windir")
                if windir:
                    cand.append(Path(windir) / path[len(prefix):])
        for c in cand:
            try:
                if not c.is_file() or (file_size and c.stat().st_size != file_size):
                    continue
                pe = PeFile(c.read_bytes())
            except (OSError, PdbError, struct.error):
                continue
            if pe.size_of_image == size and (not checksum or pe.checksum == checksum):
                return pe
        return None

    def for_image(self, read, base: int):
        """PDB of the image mapped at base (cached per base)."""
        if base in self._pdbs:
            return self._pdbs[base]
        pdb = None
        try:
            rs = rsds_from_image(read, base)
            if rs:
                pdb = self.find(*rs)
        except Exception as e:     # unreadable header, malformed PDB...
            self.log(f"[~] symboles @ {base:#x} : {e}")
        self._pdbs[base] = pdb
        return pdb


# --------------------------------------------------------------------------- #
#  Native crash-dump engine: reads Windows x64 kernel dumps (.dmp) without
#  MemProcFS - physical memory map, x64 page tables, processes, modules, threads
# --------------------------------------------------------------------------- #

PAGE = 0x1000
_PFN_MASK = 0x000FFFFFFFFFF000
_DUMP_TYPES = {1: "full", 2: "kernel (summary)", 4: "mini", 5: "bitmap (full)", 6: "live kernel (bitmap)",
               8: "kernel memory", 9: "kernel + user memory", 10: "complete memory"}


class DumpError(Exception):
    pass


class CrashDump:
    """PAGEDU64 kernel crash dump: header fields and physical memory."""

    def __init__(self, path: str):
        self.path = path
        self.f = open(path, "rb")
        self.m = mmap.mmap(self.f.fileno(), 0, access=mmap.ACCESS_READ)
        h = self.m
        if h[:8] != b"PAGEDU64":
            raise DumpError("pas un dump noyau x64 (signature PAGEDU64 attendue)")
        (self.major, self.minor, self.dtb, self.pfn_db, self.ps_loaded_module_list,
         self.ps_active_process_head, self.machine, self.processors, self.bugcheck_code) = \
            struct.unpack_from("<IIQQQQIII", h, 0x8)
        self.bugcheck_params = struct.unpack_from("<4Q", h, 0x40)
        self.kd_debugger_data_block = struct.unpack_from("<Q", h, 0x80)[0]
        self.context = bytes(h[0x348:0x348 + 0x4D0])
        self.exception = bytes(h[0xF00:0xF00 + 0x98])
        self.dump_type = struct.unpack_from("<I", h, 0xF98)[0]
        self.comment = bytes(h[0xFB0:0x1030]).split(b"\0")[0].decode("ascii", "replace")
        self._ranges = []          # sorted [(first pfn, page count, file offset)]
        self._build_ranges()
        self._starts = [r[0] for r in self._ranges]

    @property
    def type_name(self):
        return _DUMP_TYPES.get(self.dump_type, f"type {self.dump_type}")

    def _add(self, pfn, count, off):
        if count <= 0:
            return
        if self._ranges:
            lp, lc, lo = self._ranges[-1]
            if lp + lc == pfn and lo + lc * PAGE == off:
                self._ranges[-1] = (lp, lc + count, lo)
                return
        self._ranges.append((pfn, count, off))

    def _build_ranges(self):
        h = self.m
        t = self.dump_type
        if t == 1:
            nruns = struct.unpack_from("<I", h, 0x88)[0]
            off = 0x2000
            for i in range(nruns):
                base, count = struct.unpack_from("<QQ", h, 0x98 + 16 * i)
                self._add(base, count, off)
                off += count * PAGE
        elif t in (5, 6):
            if h[0x2000:0x2004] not in (b"SDMP", b"FDMP"):
                raise DumpError("en-tête bitmap inattendu")
            first, _present, pages = struct.unpack_from("<QQQ", h, 0x2020)
            bitmap = h[0x2038:0x2038 + (pages + 7) // 8]
            off = first
            pfn = 0
            n = len(bitmap)
            # walk runs of set bits, byte by byte (fast path for full / empty bytes)
            run_start = None
            for byte_i in range(n):
                byte = bitmap[byte_i]
                if byte == 0xFF and run_start is not None:
                    continue
                if byte == 0 and run_start is None:
                    continue
                for bit in range(8):
                    p = byte_i * 8 + bit
                    if byte >> bit & 1:
                        if run_start is None:
                            run_start = p
                    elif run_start is not None:
                        cnt = p - run_start
                        self._add(run_start, cnt, off)
                        off += cnt * PAGE
                        run_start = None
            if run_start is not None:
                cnt = n * 8 - run_start
                self._add(run_start, cnt, off)
        elif t in (8, 9, 10):
            marker, sig = struct.unpack_from("<I4s", h, 0x2000)
            if sig != b"RDMP":
                raise DumpError("en-tête RDMP inattendu")
            meta, first = struct.unpack_from("<QQ", h, 0x2010)
            total = struct.unpack_from("<Q", h, 0x2028)[0] if t == 10 else None
            off = first
            seen = 0
            for o in range(0, meta, 16):
                pfn, count = struct.unpack_from("<QQ", h, 0x2030 + o)
                if total is not None and seen >= total:
                    break
                if not pfn:
                    break
                self._add(pfn, count, off)
                off += count * PAGE
                seen += count
        else:
            raise DumpError(f"type de dump non pris en charge : {self.type_name}")

    # -- physical memory -- #
    def phys_page(self, pfn):
        i = bisect.bisect_right(self._starts, pfn) - 1
        if i >= 0:
            start, count, off = self._ranges[i]
            if pfn < start + count:
                o = off + (pfn - start) * PAGE
                if o + PAGE <= len(self.m):
                    return o
        return None

    def read_phys(self, pa, size):
        out = bytearray()
        while size > 0:
            o = self.phys_page(pa >> 12)
            if o is None:
                break
            take = min(size, PAGE - (pa & 0xFFF))
            out += self.m[o + (pa & 0xFFF): o + (pa & 0xFFF) + take]
            pa += take
            size -= take
        return bytes(out)

    def _q(self, pa):
        b = self.read_phys(pa, 8)
        return struct.unpack("<Q", b)[0] if len(b) == 8 else None

    def translate(self, dtb, va):
        """x64 4-level walk -> physical address, or None (not present / not in the dump)."""
        va &= 0xFFFFFFFFFFFFFFFF
        e = self._q((dtb & _PFN_MASK) + ((va >> 39) & 0x1FF) * 8)
        if e is None or not e & 1:
            return None
        e = self._q((e & _PFN_MASK) + ((va >> 30) & 0x1FF) * 8)
        if e is None or not e & 1:
            return None
        if e & 0x80:
            return (e & 0x000FFFFFC0000000) + (va & 0x3FFFFFFF)
        e = self._q((e & _PFN_MASK) + ((va >> 21) & 0x1FF) * 8)
        if e is None or not e & 1:
            return None
        if e & 0x80:
            return (e & 0x000FFFFFFFE00000) + (va & 0x1FFFFF)
        e = self._q((e & _PFN_MASK) + ((va >> 12) & 0x1FF) * 8)
        if e is None:
            return None
        # present, or a transition page (still in memory: bit 11 set, prototype bit 10 clear)
        if e & 1 or (e & 0x800 and not e & 0x400):
            return (e & _PFN_MASK) + (va & 0xFFF)
        return None

    def read_virtual(self, dtb, va, size):
        out = bytearray()
        while size > 0:
            pa = self.translate(dtb, va)
            take = min(size, PAGE - (va & 0xFFF))
            if pa is None:
                break
            chunk = self.read_phys(pa, take)
            out += chunk
            if len(chunk) < take:
                break
            va += take
            size -= take
        return bytes(out)

    def close(self):
        try:
            self.m.close()
            self.f.close()
        except Exception:
            pass




class NativeEngine:
    """Same interface as the vmm.dll engine (read / pids / process / modules / symbol /
    types / vads), implemented on a CrashDump with the kernel PDB: walks
    PsActiveProcessHead, PsLoadedModuleList and each PEB's loader list."""

    def __init__(self, path, symbols, error_cls=Exception):
        self.Err = error_cls
        self.dump = CrashDump(path)
        self.syms = symbols
        self.kdtb = self.dump.dtb
        self._procs = None
        self._kmods = None
        self._umods = {}
        km = self.kernel_modules()
        if not km:
            raise self.Err("liste des modules noyau illisible dans le dump")
        self.ntbase = km[0]["base"]
        self.pdb = symbols.for_image(lambda a, n: self._kread(a, n), self.ntbase)
        if self.pdb is None:
            raise self.Err("symboles du noyau indisponibles : le moteur natif en a besoin "
                           "(connexion au serveur de symboles Microsoft, ou --sympath)")

    # -- raw access -- #
    def _kread(self, va, size):
        return self.dump.read_virtual(self.kdtb, va, size)

    def _q(self, va, dtb=None):
        b = self.dump.read_virtual(dtb or self.kdtb, va, 8)
        return struct.unpack("<Q", b)[0] if len(b) == 8 else 0

    def _off(self, typ, field):
        r = self.pdb.field_offset(typ, field)
        if r is None:
            raise self.Err(f"champ introuvable dans les symboles : {typ}.{field}")
        return r[0]

    def _dtb_of(self, pid):
        if pid == 4:
            return self.kdtb
        p = self.process(pid)
        if not p:
            raise self.Err(f"pid {pid} introuvable")
        return p["dtb"]

    # -- Vmm-compatible API -- #
    def read(self, pid, addr, size):
        if pid == PID_PHYSICAL or pid == -1:
            data = self.dump.read_phys(addr, size)
        else:
            # kernel space is mapped in every process; session space (win32k) only in the
            # processes of a session, so use the process' own page tables when there is one
            dtb = self._dtb_of(pid) if pid != 4 else self.kdtb
            data = self.dump.read_virtual(dtb, addr, size)
            if not data and addr >= KERNEL_SPACE and dtb != self.kdtb:
                data = self.dump.read_virtual(self.kdtb, addr, size)
        if not data:
            raise self.Err(f"lecture impossible @ {addr:#x} (pid {pid}) : page absente du dump")
        return data

    def _walk_processes(self):
        out = {}
        head = self.dump.ps_active_process_head
        links = self._off("_EPROCESS", "ActiveProcessLinks")
        o_pid = self._off("_EPROCESS", "UniqueProcessId")
        o_ppid = self._off("_EPROCESS", "InheritedFromUniqueProcessId")
        o_name = self._off("_EPROCESS", "ImageFileName")
        o_peb = self._off("_EPROCESS", "Peb")
        o_dtb = self._off("_KPROCESS", "DirectoryTableBase")
        o_wow = self.pdb.field_offset("_EPROCESS", "WoW64Process")
        flink = self._q(head)
        seen = set()
        while flink and flink != head and flink not in seen and len(out) < 65536:
            seen.add(flink)
            ep = flink - links
            pid = self._q(ep + o_pid)
            name = self._kread(ep + o_name, 15).split(b"\0", 1)[0].decode("ascii", "replace")
            out[pid] = {
                "pid": pid, "ppid": self._q(ep + o_ppid), "name": name, "name_long": name,
                "eprocess": ep, "peb": self._q(ep + o_peb), "dtb": self._q(ep + o_dtb),
                "wow64": bool(self._q(ep + o_wow[0])) if o_wow else False,
                "session": 0, "integrity": "?", "sid": "", "system": "windows-x64",
            }
            flink = self._q(flink)
        return out

    def pids(self):
        if self._procs is None:
            self._procs = self._walk_processes()
        return list(self._procs)

    def process(self, pid):
        self.pids()
        return self._procs.get(pid)

    def _ldr(self, head, dtb, ts_off=0x80):
        mods = []
        flink = self._q(head, dtb)
        seen = set()
        while flink and flink != head and flink not in seen and len(mods) < 4096:
            seen.add(flink)
            ent = self.dump.read_virtual(dtb, flink, 0xA0)
            if len(ent) < 0x68:
                break
            ts = struct.unpack_from("<I", ent, ts_off)[0] if len(ent) >= ts_off + 4 else 0
            base, entry, size = struct.unpack_from("<QQI", ent, 0x30)
            fl, _a, fbuf = struct.unpack_from("<HHxxxxQ", ent, 0x48)
            ln, _b, buf = struct.unpack_from("<HHxxxxQ", ent, 0x58)
            name = self.dump.read_virtual(dtb, buf, ln).decode("utf-16-le", "replace") if ln else "?"
            path = self.dump.read_virtual(dtb, fbuf, fl).decode("utf-16-le", "replace") if fl else ""
            if base:
                mods.append({"base": base, "size": size, "entry": entry, "wow64": False,
                             "name": name, "path": path, "timestamp": ts})
            flink = struct.unpack_from("<Q", ent, 0)[0]
        return mods

    def kernel_modules(self):
        if self._kmods is None:
            self._kmods = self._ldr(self.dump.ps_loaded_module_list, self.kdtb, ts_off=0x9C)
        return self._kmods

    def modules(self, pid):
        if pid == 4:
            return list(self.kernel_modules())
        if pid not in self._umods:
            p = self.process(pid)
            if not p:
                raise self.Err(f"pid {pid} introuvable")
            mods = []
            if p["peb"]:
                ldr = self._q(p["peb"] + 0x18, p["dtb"])
                if ldr:
                    mods = self._ldr(ldr + 0x10, p["dtb"])
            self._umods[pid] = mods
        return list(self._umods[pid])

    def _module_pdb(self, module):
        low = module.lower()
        kmods = self.kernel_modules()
        for m in kmods:
            short = m["name"].lower().rsplit(".", 1)[0]
            if low in (m["name"].lower(), short) or (low == "nt" and m is kmods[0]):
                return m, self.syms.for_image(self._kread, m["base"])
        return None, None

    def symbol(self, module, name):
        m, pdb = self._module_pdb(module)
        rva = pdb.address_of(name) if pdb else None
        if rva is None:
            raise self.Err(f"symbole introuvable: {module}!{name}")
        return m["base"] + rva

    def type_size(self, module, typ):
        _m, pdb = self._module_pdb(module)
        ti = pdb.find_type(typ) if pdb else None
        if ti is None:
            raise self.Err(f"type introuvable: {module}!{typ}")
        return pdb.type_size(ti)

    def type_child_offset(self, module, typ, child):
        _m, pdb = self._module_pdb(module)
        r = pdb.field_offset(typ, child) if pdb else None
        if r is None:
            raise self.Err(f"champ introuvable: {module}!{typ}.{child}")
        return r[0]

    def vads(self, pid):
        p = self.process(pid)
        if not p:
            raise self.Err(f"pid {pid} introuvable")
        root = self._q(p["eprocess"] + self._off("_EPROCESS", "VadRoot"))
        o_s = self._off("_MMVAD_SHORT", "StartingVpn")
        o_e = self._off("_MMVAD_SHORT", "EndingVpn")
        o_sh = self.pdb.field_offset("_MMVAD_SHORT", "StartingVpnHigh")
        o_eh = self.pdb.field_offset("_MMVAD_SHORT", "EndingVpnHigh")
        out = []
        stack = [root]
        seen = set()
        while stack and len(out) < 100000:
            node = stack.pop()
            if not node or node in seen:
                continue
            seen.add(node)
            raw = self._kread(node, 0x10)
            if len(raw) < 0x10:
                continue
            left, right = struct.unpack_from("<QQ", raw, 0)
            sb, eb = self._kread(node + o_s, 4), self._kread(node + o_e, 4)
            if len(sb) < 4 or len(eb) < 4:
                continue
            s = struct.unpack("<I", sb)[0]
            e = struct.unpack("<I", eb)[0]
            if o_sh:
                s |= (self._kread(node + o_sh[0], 1) or b"\0")[0] << 32
            if o_eh:
                e |= (self._kread(node + o_eh[0], 1) or b"\0")[0] << 32
            out.append({"start": s << 12, "end": (e << 12) | 0xFFF, "vad": node, "protection": 0,
                        "image": False, "file": False, "stack": False, "text": ""})
            stack += [right, left]
        out.sort(key=lambda v: v["start"])
        return out

    def handles(self, pid):
        raise self.Err("!handle n'est pas disponible avec le moteur natif (utiliser vmm.dll)")

    def user_page_ranges(self, pid):
        """Present (or in transition) user-mode pages of a process, from its page tables:
        [(start va, size)] coalesced."""
        dtb = self._dtb_of(pid)
        d = self.dump
        ranges = []

        def add(va, size):
            if ranges and ranges[-1][0] + ranges[-1][1] == va:
                ranges[-1][1] += size
            else:
                ranges.append([va, size])
        def table(entry):
            raw = d.read_phys(entry & _PFN_MASK, PAGE)
            return struct.unpack("<512Q", raw) if len(raw) == PAGE else ()
        for i4, e4 in enumerate(table(dtb)[:256]):
            if not e4 & 1:
                continue
            for i3, e3 in enumerate(table(e4)):
                if not e3 & 1:
                    continue
                va3 = (i4 << 39) | (i3 << 30)
                if e3 & 0x80:
                    add(va3, 1 << 30)
                    continue
                for i2, e2 in enumerate(table(e3)):
                    if not e2 & 1:
                        continue
                    va2 = va3 | (i2 << 21)
                    if e2 & 0x80:
                        add(va2, 1 << 21)
                        continue
                    for i1, e1 in enumerate(table(e2)):
                        if e1 & 1 or (e1 & 0x800 and not e1 & 0x400):
                            if d.phys_page((e1 & _PFN_MASK) >> 12) is not None:
                                add(va2 | (i1 << 12), PAGE)
        return [(a, s) for a, s in ranges]

    def close(self):
        self.dump.close()


# --------------------------------------------------------------------------- #
#  Kernel debugger layer: symbols, contexts, processors, threads, x64 stack
#  unwinding (kernel -> user), shared by the vmm.dll and native dump engines
# --------------------------------------------------------------------------- #

KERNEL_SPACE = 0xFFFF800000000000
_REGS = ["rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi",
         "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"]
_VOLATILE = {"rax", "rcx", "rdx", "r8", "r9", "r10", "r11"}
# CONTEXT (x64) field offsets: fixed by the ABI
_CTX_OFF = {"rax": 0x78, "rcx": 0x80, "rdx": 0x88, "rbx": 0x90, "rsp": 0x98, "rbp": 0xA0,
            "rsi": 0xA8, "rdi": 0xB0, "r8": 0xB8, "r9": 0xC0, "r10": 0xC8, "r11": 0xD0,
            "r12": 0xD8, "r13": 0xE0, "r14": 0xE8, "r15": 0xF0, "rip": 0xF8}
CONTEXT_SIZE = 0x4D0


# --------------------------------------------------------------------------- #
#  User-mode minidump engine: MDMP files (WinDbg .dump, MiniDumpWriteDump, Task
#  Manager "Create dump file", wineye's .dump /u) - threads, modules, memory
# --------------------------------------------------------------------------- #

class MinidumpEngine:
    """Same interface as the other engines, for one process captured in a user-mode
    minidump (x64). Any pid maps to that process; reads come from the dump's memory
    ranges (full-memory dumps hold everything, small ones only the stacks)."""

    user_mode = True

    def __init__(self, path, error_cls=Exception):
        self.Err = error_cls
        self.path = path
        self.f = open(path, "rb")
        self.m = mmap.mmap(self.f.fileno(), 0, access=mmap.ACCESS_READ)
        sig, _ver, n, dir_rva = struct.unpack_from("<IIII", self.m, 0)
        if sig != _MD_SIG:
            raise self.Err("pas un minidump (signature MDMP attendue)")
        self.streams = {}
        for i in range(n):
            t, size, rva = struct.unpack_from("<III", self.m, dir_rva + 12 * i)
            self.streams.setdefault(t, (rva, size))
        si = self._stream(7)
        if si is None or struct.unpack_from("<H", si, 0)[0] != 9:
            raise self.Err("minidump non x64 (seuls les minidumps AMD64 sont pris en charge)")
        self.build = struct.unpack_from("<I", si, 16)[0]
        self.processors = si[6]
        misc = self._stream(15)
        self.pid = struct.unpack_from("<I", misc, 8)[0] if misc and struct.unpack_from("<I", misc, 4)[0] & 1 else 0
        self._ranges = []                       # sorted [(start, size, file offset)]
        self.threads = self._threads()
        self.exception = self._exception()
        self._mods = self._modules()
        self._memory()
        self._ranges.sort()
        self._starts = [r[0] for r in self._ranges]
        name = self._mods[0]["name"] if self._mods else "?"
        self._proc = {"pid": self.pid, "ppid": 0, "name": name, "name_long": name, "eprocess": 0,
                      "peb": 0, "dtb": 0, "wow64": False, "session": 0, "integrity": "?", "sid": "",
                      "system": f"windows-x64 build {self.build}"}

    def _stream(self, t):
        loc = self.streams.get(t)
        return bytes(self.m[loc[0]:loc[0] + loc[1]]) if loc else None

    def _string(self, rva):
        if not rva or rva + 4 > len(self.m):
            return ""
        n = struct.unpack_from("<I", self.m, rva)[0]
        return bytes(self.m[rva + 4:rva + 4 + n]).decode("utf-16-le", "replace")

    def _threads(self):
        out = []
        data = self._stream(3)
        if not data:
            return out
        starts = {}
        info = self._stream(17)                  # ThreadInfoList: start addresses
        if info and len(info) >= 12:
            hsize, esize, count = struct.unpack_from("<III", info, 0)
            for i in range(min(count, (len(info) - hsize) // max(esize, 1))):
                o = hsize + i * esize
                if o + 64 <= len(info):
                    starts[struct.unpack_from("<I", info, o)[0]] = struct.unpack_from("<Q", info, o + 48)[0]
        for i in range(struct.unpack_from("<I", data, 0)[0]):
            tid, _sus, _pc, _pr, teb, st_start, st_size, st_rva, c_size, c_rva = \
                struct.unpack_from("<IIIIQQIIII", data, 4 + 48 * i)
            if st_size and st_rva:          # full-memory dumps: RVA 0, data in Memory64List
                self._ranges.append((st_start, st_size, st_rva))
            out.append({"tid": tid, "teb": teb, "stack": (st_start, st_size),
                        "context": bytes(self.m[c_rva:c_rva + c_size]), "start": starts.get(tid, 0)})
        return out

    def _exception(self):
        data = self._stream(6)
        if not data or len(data) < 168:
            return None
        tid = struct.unpack_from("<I", data, 0)[0]
        code, flags, _rec, address, nparams = struct.unpack_from("<IIQQI", data, 8)
        params = list(struct.unpack_from(f"<{min(nparams, 15)}Q", data, 40))
        c_size, c_rva = struct.unpack_from("<II", data, 160)
        return {"tid": tid, "code": code, "flags": flags, "address": address, "params": params,
                "context": bytes(self.m[c_rva:c_rva + c_size])}

    def _modules(self):
        out = []
        data = self._stream(4)
        if not data:
            return out
        for i in range(struct.unpack_from("<I", data, 0)[0]):
            o = 4 + 108 * i
            base, size, csum, ts, name_rva = struct.unpack_from("<QIIII", data, o)
            path = self._string(name_rva)
            out.append({"base": base, "size": size, "entry": 0, "wow64": False,
                        "name": path.rsplit("\\", 1)[-1], "path": path, "timestamp": ts, "checksum": csum})
        return out

    def _memory(self):
        data = self._stream(9)                   # Memory64List: full-memory dumps
        if data:
            count, rva = struct.unpack_from("<QQ", data, 0)
            for i in range(count):
                start, size = struct.unpack_from("<QQ", data, 16 + 16 * i)
                self._ranges.append((start, size, rva))
                rva += size
        data = self._stream(5)                   # MemoryList: small dumps
        if data:
            for i in range(struct.unpack_from("<I", data, 0)[0]):
                start, size, rva = struct.unpack_from("<QII", data, 4 + 16 * i)
                self._ranges.append((start, size, rva))

    # -- Vmm-compatible API -- #
    def read(self, pid, addr, size):
        out = bytearray()
        while size > 0:
            i = bisect.bisect_right(self._starts, addr) - 1
            if i < 0:
                break
            start, rsize, rva = self._ranges[i]
            if addr >= start + rsize:
                break
            take = min(size, start + rsize - addr)
            out += self.m[rva + addr - start: rva + addr - start + take]
            addr += take
            size -= take
        if not out:
            raise self.Err(f"lecture impossible @ {addr:#x} : mémoire absente du minidump")
        return bytes(out)

    def pids(self):
        return [self.pid]

    def process(self, pid):
        return dict(self._proc)

    def modules(self, pid):
        return [dict(m) for m in self._mods]

    def vads(self, pid):
        return [{"start": a, "end": a + n - 1, "vad": 0, "protection": 0, "image": False, "file": False,
                 "stack": False, "text": ""} for a, n, _r in self._ranges]

    def handles(self, pid):
        raise self.Err("!handle : pas de handles dans ce minidump")

    def symbol(self, module, name):
        raise self.Err("symboles : lecteur de PDB de wineye")

    def type_size(self, module, typ):
        raise self.Err("types : lecteur de PDB de wineye")

    def type_child_offset(self, module, typ, child):
        raise self.Err("types : lecteur de PDB de wineye")

    def close(self):
        try:
            self.m.close()
            self.f.close()
        except Exception:
            pass


class DbgError(Exception):
    pass


def undecorate(name: str) -> str:
    """Readable form of an MSVC decorated name: ?Func@Class@Ns@@... -> Ns::Class::Func,
    ??0Class@@ -> Class::Class, ??1Class@@ -> Class::~Class (scope only, no signature).
    Templates and operators are left decorated."""
    if not name.startswith("?") or "@@" not in name or "?$" in name:
        return name
    parts = name[1:].split("@@", 1)[0].split("@")
    head, scope = parts[0], [p for p in parts[1:] if p]
    if head[:2] in ("?0", "?1"):
        cls = head[2:]
        return "::".join(list(reversed(scope)) + [cls, ("~" if head[1] == "1" else "") + cls])
    if head.startswith("?") or any(c.isdigit() and len(c) == 1 for c in scope):
        return name
    return "::".join(list(reversed(scope)) + [head])


def parse_context(raw: bytes) -> dict:
    """x64 CONTEXT record -> register dict."""
    if len(raw) < 0x100:
        raise DbgError("CONTEXT illisible")
    regs = {r: struct.unpack_from("<Q", raw, o)[0] for r, o in _CTX_OFF.items()}
    regs["efl"] = struct.unpack_from("<I", raw, 0x44)[0]
    regs["cs"], regs["ds"], regs["es"], regs["fs"], regs["gs"], regs["ss"] = struct.unpack_from("<6H", raw, 0x38)
    return regs


def build_context(regs: dict) -> bytes:
    """Register dict -> x64 CONTEXT record (control, integer and segment registers)."""
    buf = bytearray(CONTEXT_SIZE)
    struct.pack_into("<I", buf, 0x30, 0x10000B)    # CONTEXT_AMD64 | CONTROL | INTEGER | SEGMENTS
    struct.pack_into("<I", buf, 0x34, 0x1F80)      # MxCsr default
    struct.pack_into("<6H", buf, 0x38, regs.get("cs", 0x33), regs.get("ds", 0x2B), regs.get("es", 0x2B),
                     regs.get("fs", 0x53), regs.get("gs", 0x2B), regs.get("ss", 0x2B))
    struct.pack_into("<I", buf, 0x44, regs.get("efl", 0x246))
    for r, o in _CTX_OFF.items():
        struct.pack_into("<Q", buf, o, regs.get(r, 0) & 0xFFFFFFFFFFFFFFFF)
    struct.pack_into("<I", buf, 0x100, 0x1F80)     # FltSave.MxCsr
    return bytes(buf)


class Module:
    __slots__ = ("name", "base", "size", "path", "pdata", "exports", "pdb", "pdb_tried", "timestamp", "image",
                 "checksum", "file_size", "from_vad")

    def __init__(self, name, base, size, path="", timestamp=0, checksum=0, file_size=0, from_vad=False):
        self.name = name
        self.base = base
        self.size = size
        self.path = path
        self.pdata = None
        self.exports = None
        self.pdb = None
        self.pdb_tried = False
        self.timestamp = timestamp
        self.image = None          # PE file from the symbol server (False: not available)
        self.checksum = checksum   # PE CheckSum and file size (from the kernel's image section),
        self.file_size = file_size  # used to recognize a local copy of the image file
        self.from_vad = from_vad   # rebuilt from the VADs (loader list not in memory)

    @property
    def short(self):
        n = self.name.rsplit("\\", 1)[-1]
        low = n.lower()
        if low.startswith(("ntoskrnl", "ntkrnl", "ntkrla", "ntkrpa")):
            return "nt"
        return n.rsplit(".", 1)[0] if "." in n else n


class Frame:
    __slots__ = ("index", "rip", "rsp", "regs", "known", "module", "symbol", "line", "user", "note")

    def __init__(self, index, regs, known):
        self.index = index
        self.regs = regs
        self.known = known
        self.rip = regs["rip"]
        self.rsp = regs["rsp"]
        self.module = None
        self.symbol = None
        self.line = None
        self.user = self.rip < KERNEL_SPACE
        self.note = None


class KernelDebugger:
    """Crash-dump style analysis over a memory engine (read/pids/process/modules)."""

    def __init__(self, engine, symbols, disassembler, dump=None, log=print):
        self.e = engine
        self.syms = symbols
        self.dis = disassembler
        self.dump = dump               # CrashDump (header, context) when the target is a .dmp
        self.user_target = getattr(engine, "user_mode", False)   # user-mode minidump: threads, no kernel
        self.log = log
        self._kmods = None
        self._umods = {}
        self._nt = None
        self._off = {}
        # current state (WinDbg-like): processor, register context, thread, process
        self.cpu = 0
        self.ctx = None
        self.ctx_label = None
        self.thread = None
        self.pid = 4
        self.frames = None
        self.frame_index = 0

    # -- memory -- #
    def read(self, addr, size, pid=None):
        pid = self.pid if pid is None else pid
        try:
            data = self.e.read(pid, addr, size)
        except Exception:
            data = b""
        if len(data) < size and addr >= KERNEL_SPACE and pid != 4:
            # vmm.dll may translate a process through its user-mode DTB (KVA shadow),
            # where most of kernel space is not mapped: retry through System.
            try:
                alt = self.e.read(4, addr, size)
            except Exception:
                alt = b""
            if len(alt) > len(data):
                data = alt
        return data

    def q(self, addr, pid=None):
        b = self.read(addr, 8, pid)
        if len(b) < 8:
            raise DbgError(f"lecture impossible @ {addr:#x}")
        return struct.unpack("<Q", b)[0]

    def u32(self, addr, pid=None):
        b = self.read(addr, 4, pid)
        if len(b) < 4:
            raise DbgError(f"lecture impossible @ {addr:#x}")
        return struct.unpack("<I", b)[0]

    # -- modules -- #
    def kernel_modules(self):
        if self._kmods is None:
            mods = []
            head = None
            if self.dump is not None:
                head = self.dump.ps_loaded_module_list
            else:
                try:
                    head = self._symbol_va_via_engine("nt", "PsLoadedModuleList")
                except Exception:
                    head = None
            if head:
                mods = self._ldr_list(head, 4)
            if not mods and not self.user_target:
                for m in self.e.modules(4):
                    if m["base"] >= KERNEL_SPACE:
                        mods.append(Module(m["name"], m["base"], m["size"], m.get("path", ""), m.get("timestamp", 0)))
            mods.sort(key=lambda m: m.base)
            self._kmods = mods
        return self._kmods

    def _symbol_va_via_engine(self, mod, name):
        return self.e.symbol(mod, name)

    def _ldr_list(self, head, pid, limit=2048):
        """Walk a LIST_ENTRY of (K)LDR_DATA_TABLE_ENTRY (InLoadOrderLinks at +0)."""
        out = []
        flink = self.q(head, pid)
        seen = set()
        while flink and flink != head and flink not in seen and len(out) < limit:
            seen.add(flink)
            ent = self.read(flink, 0xA0, pid)
            if len(ent) < 0x68:
                break
            base, _ep, size = struct.unpack_from("<QQI", ent, 0x30)
            ln, _mx, buf = struct.unpack_from("<HHxxxxQ", ent, 0x58)
            fl, _mf, fbuf = struct.unpack_from("<HHxxxxQ", ent, 0x48)
            name = self.read(buf, ln, pid).decode("utf-16-le", "replace") if ln and buf else "?"
            path = self.read(fbuf, fl, pid).decode("utf-16-le", "replace") if fl and fbuf else ""
            if base:
                ts = struct.unpack_from("<I", ent, 0x9C)[0] if len(ent) >= 0xA0 else 0
                out.append(Module(name, base, size, path, ts))
            flink = struct.unpack_from("<Q", ent, 0)[0]
        return out

    def user_modules(self, pid):
        if pid not in self._umods:
            self._umods[pid] = []          # re-entrancy guard (the VAD fallback needs the nt PDB)
            mods = []
            try:
                for m in self.e.modules(pid):
                    if m["base"] < KERNEL_SPACE:
                        mods.append(Module(m["name"], m["base"], m["size"], m.get("path", ""), m.get("timestamp", 0),
                                           m.get("checksum", 0)))
            except Exception:
                pass
            if not mods:
                mods = self._vad_modules(pid)
            mods.sort(key=lambda m: m.base)
            self._umods[pid] = mods
        return self._umods[pid]

    def _vad_modules(self, pid):
        """Images mapped in a process, rebuilt from its VADs (kernel memory) when the PEB /
        loader list is not in memory - a kernel dump has no user-mode pages. The image
        section gives the file name, SizeOfImage, CheckSum and file size and, on recent
        builds, the TimeDateStamp (enough to fetch the image and its PDB)."""
        try:
            vads = self.e.vads(pid)
            off = self.off
            o_sub, o_seg = off("_MMVAD", "Subsection"), off("_CONTROL_AREA", "Segment")
            o_fp, o_flags = off("_CONTROL_AREA", "FilePointer"), off("_CONTROL_AREA", "u")
            o_name, o_info = off("_FILE_OBJECT", "FileName"), off("_SEGMENT", "u2")
            o_int = off("_MI_SECTION_IMAGE_INFORMATION", "InternalImageInformation")
            o_size = o_int + off("_MI_EXTRA_IMAGE_INFORMATION", "SizeOfImage")
            o_ts = off("_MI_EXTRA_IMAGE_INFORMATION", "TimeDateStamp", -1)
            o_csum = off("_SECTION_IMAGE_INFORMATION", "CheckSum")
            o_fsize = off("_SECTION_IMAGE_INFORMATION", "ImageFileSize")
        except Exception:
            return []
        image_bit = 5
        try:
            ti = self.ntpdb.find_type("_MMSECTION_FLAGS")
        except DbgError:
            ti = None
        for f in (self.ntpdb.fields(ti) if ti is not None else []):
            if f[0] == "Image" and f[3] is not None:
                image_bit = f[3]
        mods = []
        for v in vads:
            try:
                sub = self.q(v["vad"] + o_sub, 4)
                ca = self.q(sub, 4) if sub else 0
                if not ca or not self.u32(ca + o_flags, 4) >> image_bit & 1:
                    continue
                fo = self.q(ca + o_fp, 4) & ~0xF
                ln, buf = struct.unpack("<H6xQ", self.read(fo + o_name, 16, 4))
                path = self.read(buf, ln, 4).decode("utf-16-le", "replace") if ln else ""
                info = self.q(self.q(ca + o_seg, 4) + o_info, 4)
                raw = self.read(info, max(o_size, o_int + o_ts, o_csum, o_fsize) + 4, 4)
            except (DbgError, struct.error):
                continue

            def get(o):
                return struct.unpack_from("<I", raw, o)[0] if 0 <= o and o + 4 <= len(raw) else 0
            size = get(o_size) or (v["end"] - v["start"] + 1)
            ts = get(o_int + o_ts) if o_ts >= 0 else 0
            name = path.rsplit("\\", 1)[-1] or f"image_{v['start']:x}"
            mods.append(Module(name, v["start"], size, path, ts, get(o_csum), get(o_fsize), from_vad=True))
        return mods

    def module_at(self, addr, pid=None):
        mods = self.kernel_modules() if addr >= KERNEL_SPACE else self.user_modules(self.pid if pid is None else pid)
        bases = [m.base for m in mods]
        i = bisect.bisect_right(bases, addr) - 1
        if i >= 0 and addr < mods[i].base + mods[i].size:
            return mods[i]
        return None

    def module_by_name(self, name, pid=None):
        low = name.lower()
        for get in (self.kernel_modules, lambda: self.user_modules(self.pid if pid is None else pid)):
            for m in get():
                if m.short.lower() == low or m.name.lower() == low or m.name.lower().rsplit(".", 1)[0] == low:
                    return m
        return None

    @property
    def nt(self):
        if self._nt is None:
            m = self.module_by_name("nt")
            if m is None:
                raise DbgError("module noyau (ntoskrnl) introuvable")
            self._nt = m
        return self._nt

    def meta(self, mod, rva, size, pid=None):
        """Image metadata (headers, .pdata, unwind info, exports, CodeView record) at an RVA:
        from memory, else from the image file the symbol server holds for this module."""
        data = self.read(mod.base + rva, size, pid)
        if len(data) >= size:
            return data
        if mod.image is None:
            name = mod.name.rsplit("\\", 1)[-1]
            mod.image = self.syms.image(name, mod.timestamp, mod.size, mod.checksum, mod.file_size,
                                        mod.path) or False
        if mod.image:
            return mod.image.read(rva, size)
        return data

    def pdb(self, mod, pid=None):
        if not mod.pdb_tried:
            mod.pdb_tried = True
            p = self.pid if pid is None else pid
            mod.pdb = self.syms.for_image(lambda a, n: self.meta(mod, a - mod.base, n, p), mod.base)
        return mod.pdb

    @property
    def ntpdb(self):
        p = self.pdb(self.nt)
        if p is None:
            raise DbgError("symboles du noyau indisponibles (PDB de ntoskrnl non trouvé ni téléchargé)")
        return p

    def sym(self, name):
        """'module!symbol' or 'symbol' (nt) -> address."""
        mod, _, s = name.rpartition("!")
        m = self.module_by_name(mod) if mod else self.nt
        if m is None:
            raise DbgError(f"module inconnu : {mod}")
        p = self.pdb(m)
        rva = p.address_of(s) if p else None
        if rva is None:
            exp = self._exports(m)
            for r, n in exp:
                if n == s:
                    return m.base + r
            raise DbgError(f"symbole introuvable : {name}")
        return m.base + rva

    def off(self, typ, field, default=None):
        key = (typ, field)
        if key not in self._off:
            r = self.ntpdb.field_offset(typ, field)
            self._off[key] = r[0] if r else None
        v = self._off[key]
        if v is None:
            if default is not None:
                return default
            raise DbgError(f"champ introuvable dans les symboles : {typ}.{field}")
        return v

    # -- symbolization -- #
    def _exports(self, mod):
        if mod.exports is None:
            mod.exports = []
            p = self.pid
            try:
                rd = lambda r, n: self.meta(mod, r, n, p)
                pe = struct.unpack("<I", rd(0x3C, 4))[0]
                rva, size = struct.unpack("<II", rd(pe + 24 + 112, 8))
                if rva and size:
                    ed = rd(rva, 40)
                    (_c, _t, _ma, _mi, _nm, _base, nfunc, nnames, afunc, aname, aord) = struct.unpack("<IIHHIIIIIII", ed)
                    funcs = struct.unpack(f"<{nfunc}I", rd(afunc, 4 * nfunc))
                    names = struct.unpack(f"<{nnames}I", rd(aname, 4 * nnames))
                    ords = struct.unpack(f"<{nnames}H", rd(aord, 2 * nnames))
                    for nrva, o in zip(names, ords):
                        raw = rd(nrva, 128)
                        n = raw.split(b"\0", 1)[0].decode("ascii", "replace")
                        f = funcs[o]
                        if not (rva <= f < rva + size):     # skip forwarders
                            mod.exports.append((f, n))
                    mod.exports.sort()
            except Exception:
                pass
        return mod.exports

    def symbolize(self, addr, pid=None, with_line=False):
        """-> 'module!symbol+0x12' (and the source line from a private PDB)."""
        m = self.module_at(addr, pid)
        if m is None:
            return f"{addr:#x}", None
        rva = addr - m.base
        p = self.pdb(m, pid)
        name = None
        if p is not None:
            s = p.symbol_at(rva)
            # A separated (cold / PGO-split) chunk has no symbol of its own: its .pdata
            # entry chains to the primary function, whose symbol is the right one.
            root = self._chain_root(m, rva, pid)
            if root is not None and (s is None or rva - s[1] < root[0]):
                rs = p.symbol_at(root[1])
                if rs and rs[1] == 0:
                    s = (rs[0], rva - root[1])
            if s:
                name = f"{m.short}!{undecorate(s[0])}" + (f"+{s[1]:#x}" if s[1] else "")
        if name is None:
            exp = self._exports(m)
            rvas = [e[0] for e in exp]
            i = bisect.bisect_right(rvas, rva) - 1
            if i >= 0:
                name = f"{m.short}!{exp[i][1]}" + (f"+{rva - exp[i][0]:#x}" if rva != exp[i][0] else "")
            else:
                name = f"{m.short}+{rva:#x}"
        line = p.line_at(rva) if (with_line and p is not None) else None
        return name, line

    # -- processors / threads / contexts -- #
    def processor_count(self):
        try:
            return self.u32(self.sym("nt!KeNumberProcessors"))
        except DbgError:
            return self.dump.processors if self.dump else 1

    def prcb(self, cpu):
        return self.q(self.sym("nt!KiProcessorBlock") + 8 * cpu)

    def processor_context(self, cpu):
        """Saved register context of a processor (bugcheck / freeze): KPRCB.ProcessorState."""
        if self.dump is not None and cpu == self._crash_cpu():
            return parse_context(self.dump.context), "contexte du dump (processeur courant)"
        prcb = self.prcb(cpu)
        raw = self.read(prcb + self.off("_KPRCB", "ProcessorState.ContextFrame"), CONTEXT_SIZE, 4)
        return parse_context(raw), f"KPRCB.ProcessorState du processeur {cpu}"

    def _crash_cpu(self):
        """Processor whose state the dump header holds: the one running the context's RSP stack."""
        if not hasattr(self, "_crash_cpu_cache"):
            self._crash_cpu_cache = 0
            if self.dump is not None:
                rip = parse_context(self.dump.context)["rip"]
                try:
                    for cpu in range(self.processor_count()):
                        th = self.q(self.prcb(cpu) + self.off("_KPRCB", "CurrentThread"))
                        info = self.thread_info(th)
                        stack_lo, stack_hi = info["stack_limit"], info["initial_stack"]
                        rsp = parse_context(self.dump.context)["rsp"]
                        if stack_lo <= rsp <= stack_hi or (rip < KERNEL_SPACE and info["pid"] == self._pid_of_dtb()):
                            self._crash_cpu_cache = cpu
                            break
                except DbgError:
                    pass
        return self._crash_cpu_cache

    def _pid_of_dtb(self):
        if self.dump is None:
            return None
        for pid in self.e.pids():
            p = self.e.process(pid)
            if p and (p["dtb"] & ~0xFFF) == (self.dump.dtb & ~0xFFF):
                return pid
        return None

    def current_thread(self, cpu=None):
        return self.q(self.prcb(self.cpu if cpu is None else cpu) + self.off("_KPRCB", "CurrentThread"))

    def thread_info(self, ethread):
        off = self.off
        kt = ethread
        raw_cid = self.read(ethread + off("_ETHREAD", "Cid"), 16, 4)
        pid, tid = struct.unpack("<QQ", raw_cid) if len(raw_cid) == 16 else (0, 0)
        proc = self.q(kt + off("_KTHREAD", "ApcState.Process"))
        state = self.read(kt + off("_KTHREAD", "State"), 1, 4)
        return {
            "ethread": ethread, "pid": pid, "tid": tid, "process": proc,
            "state": state[0] if state else None,
            "kernel_stack": self.q(kt + off("_KTHREAD", "KernelStack")),
            "initial_stack": self.q(kt + off("_KTHREAD", "InitialStack")),
            "stack_limit": self.q(kt + off("_KTHREAD", "StackLimit")),
            "trap_frame": self.q(kt + off("_KTHREAD", "TrapFrame")),
            "teb": self.q(kt + off("_KTHREAD", "Teb")),
            "start": self.q(ethread + off("_ETHREAD", "StartAddress")),
            "win32_start": self.q(ethread + off("_ETHREAD", "Win32StartAddress")),
            "wait_reason": (self.read(kt + off("_KTHREAD", "WaitReason"), 1, 4) or b"\0")[0],
        }

    def process_threads(self, pid):
        p = self.e.process(pid)
        if not p:
            raise DbgError(f"pid {pid} introuvable")
        head = p["eprocess"] + self.off("_EPROCESS", "ThreadListHead")
        link_off = self.off("_ETHREAD", "ThreadListEntry")
        out = []
        flink = self.q(head)
        seen = set()
        while flink and flink != head and flink not in seen and len(out) < 4096:
            seen.add(flink)
            out.append(flink - link_off)
            flink = self.q(flink)
        return out

    def trap_frame_context(self, tf):
        """Registers saved in a KTRAP_FRAME (volatile + control; rbx/rdi/rsi when saved)."""
        regs = {}
        known = set()
        for r in ("rax", "rcx", "rdx", "r8", "r9", "r10", "r11", "rbp", "rip", "rsp", "rbx", "rdi", "rsi"):
            o = self.ntpdb.field_offset("_KTRAP_FRAME", r.capitalize() if len(r) > 2 else r.upper())
            if o is None:
                o = self.ntpdb.field_offset("_KTRAP_FRAME", r.capitalize())
            if o is not None:
                regs[r] = self.q(tf + o[0])
                known.add(r)
        o = self.ntpdb.field_offset("_KTRAP_FRAME", "EFlags")
        if o:
            regs["efl"] = self.u32(tf + o[0])
        o = self.ntpdb.field_offset("_KTRAP_FRAME", "SegCs")
        if o:
            regs["cs"] = struct.unpack("<H", self.read(tf + o[0], 2, 4))[0]
        o = self.ntpdb.field_offset("_KTRAP_FRAME", "SegSs")
        if o:
            regs["ss"] = struct.unpack("<H", self.read(tf + o[0], 2, 4))[0]
        for r in _REGS:
            regs.setdefault(r, 0)
        return regs, known

    def thread_context(self, ethread):
        """Starting register context of a thread: its processor's context if it is running,
        else the switch frame its KernelStack points to."""
        info = self.thread_info(ethread)
        try:
            for cpu in range(self.processor_count()):
                if self.current_thread(cpu) == ethread:
                    regs, label = self.processor_context(cpu)
                    return regs, set(_REGS) | {"rip"}, f"thread en cours sur le processeur {cpu}", info
        except DbgError:
            pass
        ks = info["kernel_stack"]
        ret_off = self.off("_KSWITCH_FRAME", "Return", 0x38)
        rbp_off = self.off("_KSWITCH_FRAME", "Rbp", 0x30)
        try:
            size = self.ntpdb.type_size(self.ntpdb.find_type("_KSWITCH_FRAME")) or 0x40
        except Exception:
            size = 0x40
        regs = {r: 0 for r in _REGS}
        regs["rip"] = self.q(ks + ret_off)
        regs["rsp"] = ks + size
        regs["rbp"] = self.q(ks + rbp_off)
        return regs, {"rip", "rsp", "rbp"}, "thread en attente (KernelStack / switch frame)", info

    # -- x64 unwinding -- #
    def _pdata(self, mod, pid):
        if mod.pdata is None:
            mod.pdata = []
            p = pid
            try:
                pe = struct.unpack("<I", self.meta(mod, 0x3C, 4, p))[0]
                rva, size = struct.unpack("<II", self.meta(mod, pe + 24 + 112 + 3 * 8, 8, p))
                if rva and size:
                    raw = self.meta(mod, rva, size, p)
                    n = len(raw) // 12
                    mod.pdata = [struct.unpack_from("<III", raw, 12 * i) for i in range(n)]
                    mod.pdata = [e for e in mod.pdata if e[0]]
            except Exception:
                pass
        return mod.pdata

    def function_entry(self, addr, pid):
        mod = self.module_at(addr, pid)
        if mod is None:
            return None, None
        tab = self._pdata(mod, pid)
        rva = addr - mod.base
        lo, hi = 0, len(tab) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            b, e, u = tab[mid]
            if rva < b:
                hi = mid - 1
            elif rva >= e:
                lo = mid + 1
            else:
                return mod, (b, e, u)
        return mod, None

    def _chain_root(self, mod, rva, pid):
        """(chunk begin rva, primary function begin rva) of the code containing rva, by
        following UNW_FLAG_CHAININFO; None without .pdata."""
        _m, fe = self.function_entry(mod.base + rva, pid)
        if fe is None:
            return None
        begin = fe[0]
        uw = fe[2]
        for _ in range(8):
            try:
                info = self._unwind_info(mod, uw, pid)
            except DbgError:
                break
            if info["chained"] is None:
                break
            begin_root, _end, uw = info["chained"]
            begin = begin_root
        return fe[0], begin

    def _unwind_info(self, mod, uw_rva, pid):
        p = pid
        if uw_rva & 1:       # indirect RUNTIME_FUNCTION
            b, e, u = struct.unpack("<III", self.meta(mod, uw_rva & ~1, 12, p))
            return self._unwind_info(mod, u, pid)
        hdr = self.meta(mod, uw_rva, 4, p)
        if len(hdr) < 4:
            raise DbgError("UNWIND_INFO illisible")
        ver_flags, prolog, count, fr = hdr
        codes_raw = self.meta(mod, uw_rva + 4, 2 * count + 12, p)
        codes = [struct.unpack_from("<BB", codes_raw, 2 * i) for i in range(count)]
        chained = None
        if (ver_flags >> 3) & 4:     # UNW_FLAG_CHAININFO
            o = 2 * ((count + 1) & ~1)
            chained = struct.unpack_from("<III", codes_raw, o)
        return {"version": ver_flags & 7, "prolog": prolog, "codes": codes,
                "frame_reg": fr & 0xF, "frame_off": fr >> 4, "chained": chained}

    def unwind_step(self, regs, known, pid):
        """One frame up. Returns (regs, known, crossed_machine_frame) or None at the end."""
        rip, rsp = regs["rip"], regs["rsp"]
        mod, fe = self.function_entry(rip, pid)
        rd = lambda a: self.q(a, pid)
        regs = dict(regs)
        known = set(known)
        if mod is None:
            return None
        if fe is None:
            if not self._pdata(mod, pid):    # module memory unreadable: cannot unwind
                return None
            # leaf function (no unwind data): the return address is at [rsp]
            regs["rip"] = rd(rsp)
            regs["rsp"] = rsp + 8
            return regs, known - _VOLATILE, False
        func_rva = fe[0]
        uw_rva = fe[2]
        offset = rip - (mod.base + func_rva)
        first = True
        machframe = False
        while True:
            info = self._unwind_info(mod, uw_rva, pid)
            codes = info["codes"]
            i = 0
            # frame register established by UWOP_SET_FPREG: rsp is computed from it
            while i < len(codes):
                code_off, opinfo = codes[i]
                op, inf = opinfo & 0xF, opinfo >> 4
                nslots = {0: 1, 1: 2 if inf == 0 else 3, 2: 1, 3: 1, 4: 2, 5: 3, 6: 2 if info["version"] < 2 else 1,
                          7: 3, 8: 2, 9: 3, 10: 1}.get(op, 1)
                applies = (not first) or code_off <= offset
                if applies:
                    if op == 0:                                   # UWOP_PUSH_NONVOL
                        r = _REGS[inf]
                        regs[r] = rd(regs["rsp"])
                        known.add(r)
                        regs["rsp"] += 8
                    elif op == 1:                                 # UWOP_ALLOC_LARGE
                        if inf == 0:
                            size = struct.unpack("<H", bytes(codes[i + 1]))[0] * 8
                        else:
                            size = struct.unpack("<I", bytes(codes[i + 1]) + bytes(codes[i + 2]))[0]
                        regs["rsp"] += size
                    elif op == 2:                                 # UWOP_ALLOC_SMALL
                        regs["rsp"] += inf * 8 + 8
                    elif op == 3:                                 # UWOP_SET_FPREG
                        regs["rsp"] = regs[_REGS[info["frame_reg"]]] - info["frame_off"] * 16
                    elif op == 4:                                 # UWOP_SAVE_NONVOL
                        o = struct.unpack("<H", bytes(codes[i + 1]))[0] * 8
                        r = _REGS[inf]
                        regs[r] = rd(regs["rsp"] + o)
                        known.add(r)
                    elif op == 5:                                 # UWOP_SAVE_NONVOL_FAR
                        o = struct.unpack("<I", bytes(codes[i + 1]) + bytes(codes[i + 2]))[0]
                        r = _REGS[inf]
                        regs[r] = rd(regs["rsp"] + o)
                        known.add(r)
                    elif op == 10:                                # UWOP_PUSH_MACHFRAME
                        base = regs["rsp"] + (8 if inf else 0)
                        regs["rip"] = rd(base)
                        regs["rsp"] = rd(base + 24)
                        machframe = True
                i += nslots
            if machframe:
                return regs, known - _VOLATILE, True
            if info["chained"] is None:
                break
            uw_rva = info["chained"][2]
            first = False
        regs["rip"] = rd(regs["rsp"])
        regs["rsp"] += 8
        return regs, known - _VOLATILE, False

    def stack(self, regs, known, pid=None, thread=None, max_frames=64, symbols=True, stop_at_user=False):
        """Walk the stack from a register context. Crosses kernel -> user mode through
        trap/machine frames, or through the thread's KTRAP_FRAME at the system call."""
        pid = self.pid if pid is None else pid
        frames = []
        regs = dict(regs)
        known = set(known)
        crossed_user = False
        for idx in range(max_frames):
            f = Frame(idx, regs, known)
            f.module = self.module_at(f.rip, pid)
            if symbols:
                f.symbol, f.line = self.symbolize(f.rip, pid, with_line=True)
                if idx and f.module is not None:
                    # a return address points after the call: the source line is the call's
                    p = self.pdb(f.module, pid)
                    f.line = p.line_at(f.rip - f.module.base - 1) if p is not None else None
            else:
                f.symbol = self._syscall_hint(f)
            frames.append(f)
            if stop_at_user and f.rip < KERNEL_SPACE:
                break
            try:
                nxt = self.unwind_step(regs, known, pid)
            except DbgError:
                nxt = None
            if nxt is not None:
                nregs, nknown, mach = nxt
                if mach:
                    f.note = "trap"
            # kernel -> user at the system call entry: the thread's KTRAP_FRAME holds the
            # user-mode registers (interrupts cross through their machine frame instead)
            at_syscall = f.symbol and any(s in f.symbol for s in ("KiSystemServiceCopyEnd", "KiSystemCall64",
                                                                   "KiSystemServiceUser", "KiSystemServiceExit"))
            if at_syscall and thread is not None and not crossed_user and f.rip >= KERNEL_SPACE \
                    and self._on_thread_stack(thread, f.rsp):
                tf = self.thread_info(thread)["trap_frame"]
                if tf:
                    try:
                        tregs, tknown = self.trap_frame_context(tf)
                    except DbgError:
                        tregs = None
                    if tregs and 0x10000 <= tregs["rip"] < KERNEL_SPACE:
                        # non-volatile user registers are the ones the kernel unwinding restored
                        for r in ("rbx", "rsi", "rdi", "r12", "r13", "r14", "r15", "rbp"):
                            if r in known and r not in tknown:
                                tregs[r] = regs[r]
                                tknown.add(r)
                        f.note = "user"
                        crossed_user = True
                        regs, known = tregs, tknown | {"rip", "rsp"}
                        continue
            if nxt is None:
                break
            nregs, nknown, mach = nxt
            if nregs["rip"] < 0x10000 or nregs["rsp"] <= f.rsp - 0x100000:
                break
            if f.rip >= KERNEL_SPACE and nregs["rip"] < KERNEL_SPACE:
                f.note = f.note or "user"
                crossed_user = True
            regs, known = nregs, nknown | {"rip", "rsp"}
        last = frames[-1] if frames else None
        if last is not None and last.rip < KERNEL_SPACE and len(self.read(last.rsp, 8, pid)) < 8:
            last.note = "nostack"            # the walk stops here: user stack not in memory
        return frames

    def _on_thread_stack(self, thread, rsp):
        """Is rsp inside the kernel stack of this thread (so its trap frame applies)?"""
        try:
            info = self.thread_info(thread)
        except DbgError:
            return False
        lo, hi = info["stack_limit"], info["initial_stack"]
        return bool(lo and hi and lo <= rsp <= hi + 0x1000)

    def _syscall_hint(self, f):
        """Without full symbolization, still recognize the system call entry points (from the
        nt PDB only) so the stack walk can cross to user mode."""
        if f.module is None or f.module.short != "nt":
            return None
        p = self.pdb(f.module)
        s = p.symbol_at(f.rip - f.module.base) if p else None
        return f"nt!{s[0]}" if s else None

    # -- process context of a thread -- #
    def pid_of_eprocess(self, eprocess):
        for pid in self.e.pids():
            p = self.e.process(pid)
            if p and p["eprocess"] == eprocess:
                return pid
        return None


# --------------------------------------------------------------------------- #
#  !analyze -v (bugcheck analysis) and .dump /u (user-mode minidump of a process)
# --------------------------------------------------------------------------- #

# code -> (name, description, [arg descriptions], how to find the faulting context)
#   context: ("ip", n)    arg n is the faulting instruction address
#            ("cxr", n)   arg n is a CONTEXT record
#            ("trap", n)  arg n is a KTRAP_FRAME
#            ("exr", n)   arg n is an EXCEPTION_RECORD (its ExceptionAddress)
_BUGCHECKS = {
    0x0A: ("IRQL_NOT_LESS_OR_EQUAL",
           "Accès à une adresse paginable (ou invalide) à un IRQL trop élevé.",
           ["adresse mémoire référencée", "IRQL au moment de l'accès",
            "bits : 0 lecture, 1 écriture, 8 exécution", "adresse de l'instruction fautive"], ("ip", 4)),
    0xD1: ("DRIVER_IRQL_NOT_LESS_OR_EQUAL",
           "Un pilote a accédé à une adresse paginable (ou invalide) à un IRQL trop élevé.",
           ["adresse mémoire référencée", "IRQL au moment de l'accès",
            "0 lecture, 1 écriture, 2 exécution, 8 exécution", "adresse de l'instruction fautive"], ("ip", 4)),
    0x1E: ("KMODE_EXCEPTION_NOT_HANDLED",
           "Une exception en mode noyau n'a pas été gérée.",
           ["code d'exception", "adresse de l'exception", "paramètre 0 de l'exception",
            "paramètre 1 de l'exception"], ("ip", 2)),
    0x7E: ("SYSTEM_THREAD_EXCEPTION_NOT_HANDLED",
           "Un thread système a levé une exception que le gestionnaire d'erreurs n'a pas interceptée.",
           ["code d'exception", "adresse de l'exception", "enregistrement d'exception (EXCEPTION_RECORD)",
            "enregistrement de contexte (CONTEXT)"], ("cxr", 4)),
    0x8E: ("KERNEL_MODE_EXCEPTION_NOT_HANDLED",
           "Une exception en mode noyau n'a pas été gérée.",
           ["code d'exception", "adresse de l'exception", "trap frame (KTRAP_FRAME)", "réservé"], ("trap", 3)),
    0x3B: ("SYSTEM_SERVICE_EXCEPTION",
           "Une exception s'est produite pendant l'exécution d'un service système (passage user -> noyau).",
           ["code d'exception", "adresse de l'instruction fautive", "enregistrement de contexte (CONTEXT)",
            "0"], ("cxr", 3)),
    0x50: ("PAGE_FAULT_IN_NONPAGED_AREA",
           "Une mémoire invalide a été référencée (adresse libérée ou jamais allouée).",
           ["adresse mémoire référencée", "0 lecture, 1 écriture, 2 exécution, 10 exécution",
            "adresse de l'instruction fautive (si connue)", "type de défaut de page"], ("ip", 3)),
    0x139: ("KERNEL_SECURITY_CHECK_FAILURE",
            "Une structure critique du noyau est corrompue (contrôle __fastfail : cookie de pile, LIST_ENTRY, CFG...).",
            ["type de corruption", "trap frame (KTRAP_FRAME)", "enregistrement d'exception", "réservé"], ("trap", 2)),
    0x7F: ("UNEXPECTED_KERNEL_MODE_TRAP",
           "Le processeur a généré une exception que le noyau n'a pas pu intercepter (ex. double fault, 8).",
           ["numéro de trap (0 division, 8 double fault...)", "réservé", "réservé", "réservé"], None),
    0x1A: ("MEMORY_MANAGEMENT", "Erreur grave de gestion mémoire.",
           ["sous-type", "dépend du sous-type", "dépend du sous-type", "dépend du sous-type"], None),
    0x19: ("BAD_POOL_HEADER", "En-tête de pool corrompu.",
           ["sous-type", "entrée de pool", "taille", "0"], None),
    0xC2: ("BAD_POOL_CALLER", "Requête de pool invalide d'un appelant.",
           ["sous-type", "dépend du sous-type", "dépend du sous-type", "dépend du sous-type"], None),
    0xC5: ("DRIVER_CORRUPTED_EXPOOL", "Accès à une adresse invalide à IRQL élevé : pool système corrompu.",
           ["adresse référencée", "IRQL", "0 lecture, 1 écriture", "adresse de l'instruction"], ("ip", 4)),
    0xC4: ("DRIVER_VERIFIER_DETECTED_VIOLATION", "Driver Verifier a détecté une violation.",
           ["sous-type", "dépend du sous-type", "dépend du sous-type", "dépend du sous-type"], None),
    0x9F: ("DRIVER_POWER_STATE_FAILURE", "Un pilote est dans un état d'alimentation incohérent ou invalide.",
           ["sous-code", "objet (PDO)", "dépend du sous-code", "IRP bloqué"], None),
    0xEF: ("CRITICAL_PROCESS_DIED", "Un processus critique du système s'est terminé.",
           ["objet processus (EPROCESS)", "1 si thread / 0 si processus", "réservé", "réservé"], None),
    0xF4: ("CRITICAL_OBJECT_TERMINATION", "Un processus ou thread critique s'est terminé.",
           ["type d'objet", "objet", "nom de l'image", "message"], None),
    0xE2: ("MANUALLY_INITIATED_CRASH", "Plantage déclenché volontairement (clavier, NotMyFault, .crash).",
           ["0", "0", "0", "0"], None),
    0x133: ("DPC_WATCHDOG_VIOLATION", "Un DPC ou une ISR a dépassé son temps d'exécution.",
            ["0 : un seul DPC trop long, 1 : temps cumulé trop long", "nombre de ticks", "limite", "réservé"], None),
    0x101: ("CLOCK_WATCHDOG_TIMEOUT", "Un processeur secondaire n'a pas reçu d'interruption d'horloge.",
            ["intervalle (ticks)", "0", "PRCB du processeur bloqué", "numéro du processeur"], None),
    0x124: ("WHEA_UNCORRECTABLE_ERROR", "Erreur matérielle non corrigible (WHEA).",
            ["type d'erreur", "WHEA_ERROR_RECORD", "dépend du type", "dépend du type"], None),
    0x116: ("VIDEO_TDR_FAILURE", "Le pilote graphique n'a pas répondu à temps (TDR).",
            ["objet TDR_RECOVERY_CONTEXT", "pointeur dans le pilote", "code d'erreur", "réservé"], None),
    0x109: ("CRITICAL_STRUCTURE_CORRUPTION", "PatchGuard a détecté une modification de code ou de données critiques.",
            ["réservé", "réservé", "réservé", "type de région corrompue"], None),
    0x7B: ("INACCESSIBLE_BOOT_DEVICE", "Le périphérique de démarrage est inaccessible.",
           ["objet périphérique ou chaîne", "statut", "0", "0"], None),
    0x161: ("LIVE_SYSTEM_DUMP", "Dump à chaud du système (pas un plantage).",
            ["réservé", "réservé", "réservé", "réservé"], None),
}
_EXCEPTIONS = {0xC0000005: "violation d'accès", 0x80000003: "point d'arrêt", 0x80000004: "pas à pas",
               0xC0000094: "division entière par zéro", 0xC000001D: "instruction illégale",
               0xC0000096: "instruction privilégiée", 0xC00000FD: "débordement de pile",
               0xC0000409: "dépassement de tampon / __fastfail", 0x80000002: "désalignement de données",
               0xC0000006: "erreur de page en lecture (I/O)", 0xC0000420: "échec d'assertion",
               0xC0000374: "tas corrompu"}
_FASTFAIL = {0: "FAST_FAIL_LEGACY_GS_VIOLATION", 1: "FAST_FAIL_VTGUARD_CHECK_FAILURE",
             2: "FAST_FAIL_STACK_COOKIE_CHECK_FAILURE", 3: "FAST_FAIL_CORRUPT_LIST_ENTRY",
             4: "FAST_FAIL_INCORRECT_STACK", 5: "FAST_FAIL_INVALID_ARG", 6: "FAST_FAIL_GS_COOKIE_INIT",
             7: "FAST_FAIL_FATAL_APP_EXIT", 8: "FAST_FAIL_RANGE_CHECK_FAILURE",
             9: "FAST_FAIL_UNSAFE_REGISTRY_ACCESS", 10: "FAST_FAIL_GUARD_ICALL_CHECK_FAILURE",
             13: "FAST_FAIL_INVALID_BUFFER_ACCESS", 14: "FAST_FAIL_INVALID_BALANCED_TREE",
             15: "FAST_FAIL_INVALID_NEXT_THREAD", 18: "FAST_FAIL_INVALID_FIBER_SWITCH",
             37: "FAST_FAIL_GUARD_SS_FAILURE"}
_NT_MODULES = ("nt", "hal", "kdcom", "kd")


def bugcheck_name(code):
    base = code & 0xFFFFFFF if code & 0x10000000 and (code & 0xFFFFFFF) in _BUGCHECKS else code
    entry = _BUGCHECKS.get(base)
    return entry[0] if entry else f"BUGCHECK_{code:X}"


class BugcheckAnalyzer:
    """!analyze -v: decodes the stop code and its arguments, recovers the faulting
    context (trap frame / context record / instruction), names the faulting
    instruction, module and symbol, and prints the stack."""

    def __init__(self, kd):
        self.kd = kd

    def bugcheck_data(self):
        kd = self.kd
        if kd.dump is not None and kd.dump.bugcheck_code:
            return kd.dump.bugcheck_code, list(kd.dump.bugcheck_params), "en-tête du dump"
        try:
            base = kd.sym("nt!KiBugCheckData")
            vals = struct.unpack("<5Q", kd.read(base, 40, 4))
            return vals[0] & 0xFFFFFFFF, list(vals[1:]), "nt!KiBugCheckData"
        except Exception:
            return 0, [0, 0, 0, 0], None

    def run(self, verbose=True):
        kd = self.kd
        if kd.user_target:
            return self._exception_report()
        out = []
        w = out.append
        code, args, source = self.bugcheck_data()
        w("*" * 79)
        w("*" + "Analyse du bugcheck".center(77) + "*")
        w("*" * 79)
        if not code:
            w("")
            w("Aucun bugcheck : ce dump n'est pas un plantage (dump pris par .dump, dump à chaud,")
            w("ou image mémoire). Analyse du contexte du processeur courant.")
            ctx_regs, label = kd.processor_context(kd.cpu)
            th = None
            try:
                th = kd.current_thread(kd.cpu)
            except Exception:
                pass
            return out + self._context_report(ctx_regs, set(ctx_regs), label, th, None, None, code)
        base = code & 0xFFFFFFF if code & 0x10000000 and (code & 0xFFFFFFF) in _BUGCHECKS else code
        entry = _BUGCHECKS.get(base)
        name = entry[0] if entry else f"BUGCHECK_{code:X}"
        w("")
        w(f"{name} ({code:x})")
        if entry:
            w(entry[1])
        w("Arguments :")
        for i, a in enumerate(args, 1):
            desc = entry[2][i - 1] if entry else ""
            extra = ""
            if i == 1 and base in (0x1E, 0x7E, 0x8E, 0x3B):
                extra = f" ({_EXCEPTIONS.get(a & 0xFFFFFFFF, 'exception')})"
            if i == 1 and base == 0x139:
                extra = f" ({_FASTFAIL.get(a, '?')})"
            w(f"Arg{i}: {a:016x}, {desc}{extra}")
        w(f"(source : {source})")
        regs = known = None
        label = None
        fault_ip = None
        how = entry[3] if entry else None
        exc_code = args[0] & 0xFFFFFFFF if base in (0x1E, 0x7E, 0x8E, 0x3B) else None
        try:
            if how and how[0] == "cxr" and args[how[1] - 1]:
                from_raw = kd.read(args[how[1] - 1], 0x4D0, 4)
                regs = kd_parse_context(from_raw)
                known = set(regs)
                label = f".cxr {args[how[1] - 1]:#x}"
            elif how and how[0] == "trap" and args[how[1] - 1]:
                regs, known = kd.trap_frame_context(args[how[1] - 1])
                label = f".trap {args[how[1] - 1]:#x}"
            elif how and how[0] == "ip" and args[how[1] - 1]:
                fault_ip = args[how[1] - 1]
        except Exception as e:
            w(f"[~] contexte fautif illisible : {e}")
        if regs is None:
            regs, label = kd.processor_context(kd.cpu)
            known = set(regs)
        if fault_ip is None and regs is not None and label and label.startswith((".cxr", ".trap")):
            fault_ip = regs["rip"]
        th = None
        try:
            th = kd.current_thread(kd.cpu)
        except Exception:
            pass
        return out + self._context_report(regs, known, label, th, fault_ip, exc_code, code)

    def _exception_report(self):
        """User-mode minidump: the exception stream (code, address, parameters, context
        record of the faulting thread), else the current thread."""
        kd = self.kd
        e = kd.e
        out = ["*" * 79, "*" + "Analyse de l'exception".center(77) + "*", "*" * 79, ""]
        w = out.append
        exc = e.exception
        name = e.process(e.pid)["name"]
        if exc is None or not exc["code"]:
            w(f"Aucune exception dans ce minidump ({name}, pid {e.pid}) : analyse du thread courant.")
            regs, label = kd.ctx or parse_context(e.threads[0]["context"]), kd.ctx_label or "thread 0"
            return out + self._context_report(regs, set(regs), label, None, None, None, 0)
        code = exc["code"]
        w(f"PROCESS_NAME : {name}  (pid {e.pid})")
        w(f"EXCEPTION_RECORD : code {code:08x} ({_EXCEPTIONS.get(code, 'exception')}), "
          f"adresse {kd.symbolize(exc['address'], e.pid)[0]}, thread {exc['tid']:#x}")
        if code == 0xC0000005 and len(exc["params"]) >= 2:
            op = {0: "lecture", 1: "écriture", 8: "exécution"}.get(exc["params"][0], str(exc["params"][0]))
            w(f"  accès en {op} à l'adresse {exc['params'][1]:#018x}")
        elif exc["params"]:
            w("  paramètres : " + " ".join(f"{x:#x}" for x in exc["params"]))
        regs = parse_context(exc["context"]) if len(exc["context"]) >= 0x100 else None
        label = f"contexte de l'exception (thread {exc['tid']:#x})"
        if regs is None:
            idx = next((i for i, t in enumerate(e.threads) if t["tid"] == exc["tid"]), 0)
            regs, label = parse_context(e.threads[idx]["context"]), f"thread {idx}"
        out += self._context_report(regs, set(regs), label, None, exc["address"], code, 0)
        sym = next((x.split(" : ", 1)[1] for x in out if x.startswith("SYMBOL_NAME : ")), None)
        if sym:
            w(f"FAILURE_BUCKET_ID : {code:08X}_{sym}")
        return out

    def _context_report(self, regs, known, label, thread, fault_ip, exc_code, code):
        kd = self.kd
        out = []
        w = out.append
        if exc_code is not None:
            w("")
            w(f"EXCEPTION_CODE: {exc_code:08x} ({_EXCEPTIONS.get(exc_code, 'exception')})")
        if label:
            w(f"CONTEXTE : {label}")
        pid = kd.pid
        if thread:
            try:
                info = kd.thread_info(thread)
                pid = kd.pid_of_eprocess(info["process"]) or pid
                proc = kd.e.process(pid)
                w(f"THREAD : {thread:#x}  (pid {info['pid']}, tid {info['tid']}"
                  + (f", {proc['name']}" if proc else "") + ")")
            except Exception:
                pass
        frames = kd.stack(regs, known, pid=pid, thread=thread, max_frames=40)
        ip = fault_ip if fault_ip is not None else (frames[0].rip if frames else None)
        if ip is not None:
            sym, line = kd.symbolize(ip, pid, with_line=True)
            w("")
            w(f"FAULTING_IP : {sym}")
            code_bytes = kd.read(ip, 16, pid)
            if code_bytes:
                ins = kd.dis.decode(code_bytes, ip)
                w(f"  {ip:016x} {ins.raw.hex():<20} {ins.text}")
            if line:
                w(f"  source : {line[0]} @ {line[1]}")
        w("")
        w("STACK_TEXT :")
        w(f"  {'#':<3} {'Child-SP':<17} {'RetAddr':<17} Site d'appel")
        for i, f in enumerate(frames):
            ret = frames[i + 1].rip if i + 1 < len(frames) else 0
            w(f"  {f.index:02x}  {f.rsp:016x}  {ret:016x}  {f.symbol}"
              + (f"  [{f.line[0].rsplit(chr(92), 1)[-1]} @ {f.line[1]}]" if f.line else ""))
            if f.note == "user" and i + 1 < len(frames):
                w("       --- passage en mode utilisateur ---")
            elif f.note == "nostack":
                w("       " + "(suite de la pile user-mode absente : la source ne contient pas la mémoire utilisateur)")
        # faulting module: the faulting IP's module, else the first frame outside nt/hal
        culprit = None
        if ip is not None:
            culprit = kd.module_at(ip, pid)
        if culprit is None or culprit.short.lower() in _NT_MODULES:
            for f in frames:
                if f.module is not None and f.module.short.lower() not in _NT_MODULES:
                    culprit = f.module
                    ip = f.rip
                    break
        if culprit is not None:
            sym = kd.symbolize(ip, pid)[0]
            w("")
            w(f"MODULE_NAME : {culprit.short}")
            w(f"IMAGE_NAME : {culprit.name}")
            w(f"SYMBOL_NAME : {sym.split('+', 1)[0]}")
            if code:
                w(f"FAILURE_BUCKET_ID : {bugcheck_name(code)}_{sym.split('+', 1)[0]}")
        return out


def kd_parse_context(raw):
    return parse_context(raw)


# --------------------------------------------------------------------------- #
#  .dump /u : user-mode minidump of a process, rebuilt from kernel memory
# --------------------------------------------------------------------------- #
_MD_SIG = 0x504D444D


def _md_string(text):
    raw = text.encode("utf-16-le")
    return struct.pack("<I", len(raw)) + raw + b"\0\0"


def write_user_minidump(kd, pid, path, log=print):
    """Writes a full-memory minidump of process pid: threads (user-mode registers recovered
    from the trap frame and the kernel stack unwinding, TEB, stack), modules (base, size,
    timestamp, CodeView record) and every user-mode page present in memory."""
    proc = kd.e.process(pid)
    if not proc:
        raise ValueError(f"pid {pid} introuvable")
    saved_pid = kd.pid
    kd.pid = pid
    try:
        # -- memory ranges -- #
        if hasattr(kd.e, "user_page_ranges"):
            ranges = kd.e.user_page_ranges(pid)
        else:
            ranges = [(v["start"], v["end"] - v["start"] + 1) for v in kd.e.vads(pid)]
        # -- threads: user-mode register context -- #
        threads = []
        for th in kd.process_threads(pid):
            info = kd.thread_info(th)
            try:
                regs, known, _label, _ = kd.thread_context(th)
                frames = kd.stack(regs, known, pid=pid, thread=th, max_frames=64, symbols=False, stop_at_user=True)
            except Exception:
                frames = []
            user = next((f for f in frames if f.rip < KERNEL_SPACE), None)
            if user is None:
                continue
            ctx = dict(user.regs)
            ctx.setdefault("cs", 0x33)
            ctx.setdefault("ss", 0x2B)
            stack_base = 0
            if info["teb"]:
                try:
                    stack_base = kd.q(info["teb"] + 8, pid)
                except Exception:
                    stack_base = 0
            threads.append((info, build_context(ctx), user.rsp, stack_base))
        # -- modules -- #
        mods = []
        for m in kd.user_modules(pid):
            cv = b""
            try:
                rs = rsds_from_image(lambda a, n: kd.read(a, n, pid), m.base)   # memory only: no downloads
                if rs:
                    name, key, pdbpath = rs
                    g_age = bytes.fromhex(key[:32])
                    d1, d2, d3 = int(key[0:8], 16), int(key[8:12], 16), int(key[12:16], 16)
                    guid = struct.pack("<IHH", d1, d2, d3) + bytes.fromhex(key[16:32])
                    cv = b"RSDS" + guid + struct.pack("<I", int(key[32:], 16)) + pdbpath.encode() + b"\0"
            except Exception:
                cv = b""
            mods.append((m, cv))
        # -- layout: header, directory, small streams, thread list, memory list, page data -- #
        import bisect
        n_dir = 5
        blob = bytearray()
        base_off = 32 + 12 * n_dir

        def place(data):
            rva = base_off + len(blob)
            blob.extend(data)
            return rva
        dump = kd.dump
        sysinfo = bytearray(struct.pack("<HHHBBIIIIIHH", 9, 6, 0, kd.processor_count() & 0xFF, 1, 10, 0,
                                        dump.minor if dump else 0, 2, 0, 0, 0) + b"\0" * 24)
        sys_rva = place(bytes(sysinfo))
        csd_rva = place(_md_string(""))
        struct.pack_into("<I", blob, sys_rva - base_off + 24, csd_rva)
        name_rvas = [place(_md_string(m.path or m.name)) for m, _cv in mods]
        cv_refs = [(place(cv), len(cv)) if cv else (0, 0) for _m, cv in mods]
        ctx_rvas = [place(ctx) for _info, ctx, _rsp, _sb in threads]
        ml = bytearray(struct.pack("<I", len(mods)))
        for (m, _cv), nrva, (cvrva, cvsize) in zip(mods, name_rvas, cv_refs):
            if not m.timestamp and m.image is None and m.from_vad:
                # kernel dump: a local copy of the image (no download here) gives the
                # TimeDateStamp that debuggers need to fetch the image and its symbols
                m.image = kd.syms.image(m.name, 0, m.size, m.checksum, m.file_size, m.path) or None
            ts = m.timestamp or (m.image.timestamp if m.image else 0)
            ml += struct.pack("<QIIII", m.base, m.size, m.checksum, ts, nrva)
            ml += b"\0" * 52                          # VS_FIXEDFILEINFO (not recovered)
            ml += struct.pack("<IIII", cvsize, cvrva, 0, 0) + struct.pack("<QQ", 0, 0)
        ml_rva = place(bytes(ml))
        misc = struct.pack("<IIIIII", 24, 1, pid, 0, 0, 0)
        misc_rva = place(misc)
        tl_size = 4 + 48 * len(threads)
        tl_rva = place(b"\0" * tl_size)                # filled once the page data offsets are known
        ml64 = bytearray(struct.pack("<QQ", len(ranges), 0))
        for start, size in ranges:
            ml64 += struct.pack("<QQ", start, size)
        mem_rva = place(bytes(ml64))
        data_rva = base_off + len(blob)
        struct.pack_into("<Q", blob, mem_rva - base_off + 8, data_rva)
        range_rva = []
        cur = data_rva
        for _start, size in ranges:
            range_rva.append(cur)
            cur += size
        starts = [r[0] for r in ranges]
        tl = bytearray(struct.pack("<I", len(threads)))
        for (info, _ctx, rsp, stack_base), crva in zip(threads, ctx_rvas):
            s_rva = s_size = 0
            i = bisect.bisect_right(starts, rsp) - 1
            if i >= 0 and rsp < ranges[i][0] + ranges[i][1]:
                end = ranges[i][0] + ranges[i][1]
                if stack_base and rsp < stack_base <= end:
                    end = stack_base
                s_size = end - rsp
                s_rva = range_rva[i] + (rsp - ranges[i][0])
            tl += struct.pack("<IIIIQQIIII", info["tid"] & 0xFFFFFFFF, 0, 0x20, 8, info["teb"],
                              rsp, s_size, s_rva, 0x4D0, crva)
        blob[tl_rva - base_off: tl_rva - base_off + tl_size] = tl
        directory = [(7, len(sysinfo), sys_rva), (3, tl_size, tl_rva), (4, len(ml), ml_rva),
                     (15, len(misc), misc_rva), (9, len(ml64), mem_rva)]
        header = struct.pack("<IIIIIIQ", _MD_SIG, 0xA793, len(directory), 32, 0, int(time.time()), 0x2)
        dir_bytes = b"".join(struct.pack("<III", t, sz, r) for t, sz, r in directory)
        total = 0
        with open(path, "wb") as f:
            f.write(header + dir_bytes)
            f.write(blob)
            for start, size in ranges:
                done = 0
                while done < size:
                    n = min(size - done, 0x100000)
                    chunk = kd.read(start + done, n, pid)
                    if len(chunk) < n:
                        chunk = chunk + b"\0" * (n - len(chunk))
                    f.write(chunk)
                    done += n
                total += size
        return {"threads": len(threads), "modules": len(mods), "ranges": len(ranges), "bytes": total}
    finally:
        kd.pid = saved_pid


# --------------------------------------------------------------------------- #
#  REPL debugger commands (k, ~, !thread, .thread, .cxr, .trap, r, u, uf, ln,
#  .frame, dv, !analyze, .dump /u, .sympath, .reload)
# --------------------------------------------------------------------------- #
_THREAD_STATES = ["Initialized", "Ready", "Running", "Standby", "Terminated", "Waiting", "Transition",
                  "DeferredReady", "GateWaitObsolete", "WaitingForProcessInSwap"]
_WAIT_REASONS = ["Executive", "FreePage", "PageIn", "PoolAllocation", "DelayExecution", "Suspended",
                 "UserRequest", "WrExecutive", "WrFreePage", "WrPageIn", "WrPoolAllocation",
                 "WrDelayExecution", "WrSuspended", "WrUserRequest", "WrSpare0", "WrQueue",
                 "WrLpcReceive", "WrLpcReply", "WrVirtualMemory", "WrPageOut", "WrRendezvous",
                 "WrKeyedEvent", "WrTerminated", "WrProcessInSwap", "WrCpuRateControl",
                 "WrCalloutStack", "WrKernel", "WrResource", "WrPushLock", "WrMutex", "WrQuantumEnd",
                 "WrDispatchInt", "WrPreempted", "WrYieldExecution", "WrFastMutex", "WrGuardedMutex",
                 "WrRundown", "WrAlertByThreadId", "WrDeferredPreempt", "WrPhysicalFault", "WrIoRing",
                 "WrMdlCache", "WrRcu"]
_CTX_REGS = ["rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rip", "rsp", "rbp",
             "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"]


class DebugCommands:
    """Mixed into Repl: needs self.vmm, self.kd (KernelDebugger or None), self.pid,
    self.json, self.ctl."""

    # -- current context -- #
    def _mem(self, addr, size, pid=None):
        """Memory read for the commands: through the debugger layer when present (it
        falls back to System for kernel addresses), raising VmmError when unreadable."""
        pid = self.pid if pid is None else pid
        if self.kd is not None:
            data = self.kd.read(addr, size, pid)
            if data:
                return data
        return self.vmm.read(pid, addr, size)

    def _need_kd(self) -> bool:
        if self.kd is None:
            print("[!] commandes de débogage indisponibles : pas de couche noyau (symboles)")
            return False
        self.kd.pid = self.pid
        return True

    def _user_thread(self, i, quiet=False):
        kd = self.kd
        t = kd.e.threads[i]
        exc = kd.e.exception
        raw = exc["context"] if exc and exc["tid"] == t["tid"] and len(exc["context"]) >= 0x100 else t["context"]
        regs = parse_context(raw)
        kd.cpu = i
        label = f"thread {i} (tid {t['tid']:#x})" + (" - contexte de l'exception" if raw is not t["context"] else "")
        if quiet:
            kd.ctx, kd.ctx_known, kd.ctx_label, kd.thread = regs, set(regs), label, None
            kd.frames, kd.frame_index = None, 0
            self.pid = kd.pid = kd.e.pid
        else:
            self._set_context(regs, set(regs), label, None, kd.e.pid)

    def _ensure_context(self):
        kd = self.kd
        if kd.ctx is None and kd.user_target:
            exc = kd.e.exception
            idx = next((i for i, t in enumerate(kd.e.threads) if exc and t["tid"] == exc["tid"]), 0)
            self._user_thread(idx, quiet=True)
        if kd.ctx is None:
            cpu = kd._crash_cpu() if kd.dump is not None else 0
            kd.cpu = cpu
            regs, label = kd.processor_context(cpu)
            kd.ctx, kd.ctx_known, kd.ctx_label = regs, set(regs), label
            try:
                kd.thread = kd.current_thread(cpu)
                pid = kd.pid_of_eprocess(kd.thread_info(kd.thread)["process"])
                if pid is not None:
                    self.pid = kd.pid = pid
            except Exception:
                kd.thread = None
        return kd.ctx

    def _set_context(self, regs, known, label, thread=None, pid=None):
        kd = self.kd
        kd.ctx, kd.ctx_known, kd.ctx_label = regs, set(known), label
        kd.thread = thread
        kd.frames = None
        kd.frame_index = 0
        if pid is not None:
            self.pid = kd.pid = pid
        print(f"contexte : {label}  (rip {regs['rip']:#x}, rsp {regs['rsp']:#x})")

    def _frames(self):
        kd = self.kd
        self._ensure_context()
        if kd.frames is None:
            kd.frames = kd.stack(kd.ctx, kd.ctx_known, pid=self.pid, thread=kd.thread)
        return kd.frames

    # -- expressions: registers, module!symbol, hex (WinDbg style), + and - -- #
    def eval_expr(self, text: str) -> int:
        text = text.strip().replace("`", "")
        total = 0
        sign = 1
        token = ""
        for ch in text + "+":
            if ch in "+-" and token and not token.endswith("!"):
                total += sign * self._eval_term(token.strip())
                sign = 1 if ch == "+" else -1
                token = ""
            else:
                token += ch
        return total & 0xFFFFFFFFFFFFFFFF

    def _eval_term(self, t: str) -> int:
        low = t.lower().lstrip("@")
        if self.kd is not None and low in _CTX_REGS + ["efl"]:
            self._ensure_context()
            regs = self.kd.ctx
            if self.kd.frames and self.kd.frame_index:
                regs = self.kd.frames[self.kd.frame_index].regs
            return regs.get(low, 0)
        if "!" in t:
            mod, sym = t.split("!", 1)
            try:
                return self.vmm.symbol(mod, sym)
            except Exception:
                if self.kd is None:
                    raise
                return self.kd.sym(t)
        if low.startswith("0n"):
            return int(low[2:], 10)
        if low.startswith(("0x", "0o", "0b")):
            return int(low, 0)
        try:
            return int(low, 16)          # WinDbg: numbers are hexadecimal by default
        except ValueError:
            if self.kd is not None:
                return self.kd.sym(t)
            raise

    # -- k / .frame / dv -- #
    def _home_args(self, frames, i):
        """The 4 parameter home slots of frame i (x64: just above its return address, i.e.
        at the caller's Child-SP) - what WinDbg's kb shows as "Args to Child". Valid only
        where the function spilled RCX/RDX/R8/R9 there (always with /Od, often not when
        optimized: the slots are then scratch space)."""
        if i + 1 >= len(frames) or frames[i].note in ("user", "trap"):
            return None
        raw = self.kd.read(frames[i + 1].rsp, 32, self.pid)
        return list(struct.unpack("<4Q", raw)) if len(raw) == 32 else None

    def _cmd_k(self, args, args_to_child=False):
        if not self._need_kd():
            return
        frames = self._frames()
        count = int(args[0], 0) if args and args[0].isdigit() else len(frames)
        if self.json:
            print(_jdump([{"index": f.index, "rsp": f.rsp, "rip": f.rip, "symbol": f.symbol,
                           "file": f.line[0] if f.line else None, "line": f.line[1] if f.line else None,
                           "transition": f.note,
                           **({"args_to_child": self._home_args(frames, i)} if args_to_child else {})}
                          for i, f in enumerate(frames[:count])]))
            return
        print(f"contexte : {self.kd.ctx_label}")
        if args_to_child:
            print(f" #  {'Child-SP':<17} {'RetAddr':<17} : {'Args to Child':<71} : Site d'appel")
        else:
            print(f" #  {'Child-SP':<17} {'RetAddr':<17} Site d'appel")
        for i, f in enumerate(frames[:count]):
            ret = frames[i + 1].rip if i + 1 < len(frames) else 0
            src = f"  [{f.line[0]} @ {f.line[1]}]" if f.line else ""
            if args_to_child:
                ha = self._home_args(frames, i)
                cols = " ".join(f"{a:016x}" for a in ha) if ha else " ".join(["?" * 16] * 4)
                print(f"{f.index:02x}  {f.rsp:016x}  {ret:016x}  : {cols} : {f.symbol}{src}")
            else:
                print(f"{f.index:02x}  {f.rsp:016x}  {ret:016x}  {f.symbol}{src}")
            if f.note == "user" and i + 1 < len(frames):
                print("    --- passage en mode utilisateur ---")
            elif f.note == "trap" and i + 1 < len(frames):
                print("    --- trap / interruption ---")
            elif f.note == "nostack":
                print("    " + "(suite de la pile user-mode absente : la source ne contient pas la mémoire utilisateur)")

    def _cmd_frame(self, args):
        if not self._need_kd():
            return
        frames = self._frames()
        if args:
            n = int(args[0], 16)
            if not 0 <= n < len(frames):
                print(f"[!] frame {n:x} hors de la pile ({len(frames)} frames)")
                return
            self.kd.frame_index = n
        f = frames[self.kd.frame_index]
        src = f"  [{f.line[0]} @ {f.line[1]}]" if f.line else ""
        print(f"{f.index:02x} {f.rsp:016x} {f.symbol}{src}")

    def _cmd_dv(self, args):
        if not self._need_kd():
            return
        kd = self.kd
        frames = self._frames()
        f = frames[kd.frame_index]
        if f.module is None:
            print("[!] module inconnu pour cette frame")
            return
        pdb = kd.pdb(f.module, self.pid)
        if pdb is None:
            self._dv_public(frames, kd.frame_index, f"pas de PDB pour {f.module.short}")
            return
        rva = f.rip - f.module.base - (1 if f.index else 0)
        fname, variables = pdb.locals_at(rva)
        # public PDBs (Microsoft's) may keep procedure records, but without variables or lines
        if fname is None or not (variables or pdb.line_at(rva)):
            self._dv_public(frames, kd.frame_index)
            return
        seen = set()
        rows = []
        for v in variables:
            if v["name"] in seen or v["where"] is None:
                continue
            value, addr = self._local_value(pdb, f, v)
            if value is None and any(x["name"] == v["name"] and x is not v and x["where"] for x in variables):
                continue
            seen.add(v["name"])
            rows.append((v, value, addr))
        if self.json:
            print(_jdump([{"name": v["name"], "type": pdb.type_name(v["type"]), "param": v["param"],
                           "address": addr, "value": val} for v, val, addr in rows]))
            return
        print(f"{fname}  (frame {f.index:02x})")
        if not rows:
            print("  (ni paramètre ni variable locale)")
        for v, value, addr in rows:
            kind = "param" if v["param"] else "local"
            where = f"@ {addr:016x}" if addr is not None else f"@ {v['where'][1]}"
            print(f"  {kind:<6} {pdb.type_name(v['type']):<22} {v['name']:<20} = {value if value is not None else '<indisponible>'}   {where}")

    def _dv_public(self, frames, i, reason=None):
        """dv without a private PDB: names and types of locals are not in a public PDB, so
        show what the stack still holds - the 4 parameter home slots - and, at the first
        frame, the argument registers."""
        f = frames[i]
        ha = self._home_args(frames, i)
        regs = [f.regs.get(r) if r in f.known else None for r in ("rcx", "rdx", "r8", "r9")] if i == 0 else None
        if self.json:
            print(_jdump({"function": f.symbol, "private_pdb": False, "home_slots": ha, "arg_registers": regs}))
            return
        print(f"[!] {reason or 'PDB public pour ' + f.module.short} : pas de noms ni de types de variables "
              f"locales (il faut le PDB privé, voir .sympath)")
        print(f"{f.symbol}  (frame {f.index:02x})")
        if ha:
            print("  slots de paramètres (home space) - valables si la fonction y a rangé ses arguments,")
            print("  ce que fait tout code /Od ; en code optimisé, ce peut être autre chose :")
            for n, a in enumerate(ha, 1):
                sym = self.kd.symbolize(a, self.pid)[0] if a >= 0x10000 and self.kd.module_at(a, self.pid) else ""
                print(f"    arg{n}  {a:016x}  {sym}")
        else:
            print("  slots de paramètres illisibles (pas de frame appelante en mémoire)")
        if regs and any(v is not None for v in regs):
            print("  registres d'arguments à cet instant (= paramètres seulement à l'entrée de la fonction) :")
            for r, v in zip(("rcx", "rdx", "r8", "r9"), regs):
                print(f"    {r:<4}  {v:016x}" if v is not None else f"    {r:<4}  ?")

    def _local_value(self, pdb, f, v):
        kd = self.kd
        where = v["where"]
        size = pdb.type_size(v["type"]) or 8
        if where[0] == "regrel":
            reg = where[1]
            base = f.regs.get(reg)
            if base is None or (reg not in f.known and reg not in ("rsp",)):
                return None, None
            addr = (base + where[2]) & 0xFFFFFFFFFFFFFFFF
            raw = kd.read(addr, min(size, 64), self.pid)
            if not raw:
                return None, addr
            return self._with_string(pdb, v["type"], raw, self._format_value(pdb, v["type"], raw, size)), addr
        if where[0] == "reg":
            reg = where[1]
            full = {"eax": "rax", "ebx": "rbx", "ecx": "rcx", "edx": "rdx", "esi": "rsi", "edi": "rdi",
                    "ebp": "rbp", "esp": "rsp"}.get(reg, reg.rstrip("d") if reg.endswith("d") and reg.startswith("r") else reg)
            if full not in f.known:
                return None, None
            val = f.regs.get(full, 0) & ((1 << (8 * min(size, 8))) - 1)
            raw = val.to_bytes(8, "little")[:min(size, 8)]
            return self._with_string(pdb, v["type"], raw, self._format_value(pdb, v["type"], raw, size)), None
        return None, None

    def _with_string(self, pdb, ti, raw, text):
        """char * / wchar_t * : append the string it points to, as WinDbg does."""
        name = pdb.type_name(ti).replace("const ", "")
        if text is None or len(raw) < 8 or name not in ("char *", "wchar_t *", "unsigned short *"):
            return text
        ptr = int.from_bytes(raw[:8], "little")
        data = self.kd.read(ptr, 256, self.pid) if ptr >= 0x10000 else b""
        if not data:
            return text
        if name == "char *":
            out = data.split(b"\0", 1)[0].decode("latin-1")
        else:
            out = data[:len(data) & ~1].decode("utf-16-le", "replace").split("\0", 1)[0]
        return f"{text} {out!r}"

    @staticmethod
    def _format_value(pdb, ti, raw, size, depth=0):
        kind = pdb.type_kind(ti)
        name = pdb.type_name(ti)
        if kind in ("struct", "union") and depth < 2:
            fields = pdb.fields(ti)
            parts = []
            for fname, foff, fti, bpos, _blen in fields[:8]:
                fsize = pdb.type_size(fti) or 0
                if bpos is not None or not fsize or foff + fsize > len(raw):
                    continue
                val = DebugCommands._format_value(pdb, fti, raw[foff:foff + fsize], fsize, depth + 1)
                dec, _, hx = val.partition("  (")
                if hx and dec.startswith("0n"):          # small integers in decimal, others in hex
                    val = dec[2:] if -0x10000 < int(dec[2:]) < 0x10000 else hx.rstrip(")")
                parts.append(f"{fname}={val}")
            if parts:
                return "{" + " ".join(parts) + (" ..." if len(fields) > 8 or size > len(raw) else "") + "}"
        if kind in ("base", "pointer", "enum", "modifier", "bitfield") and size in (1, 2, 4, 8) and len(raw) >= size:
            v = int.from_bytes(raw[:size], "little", signed=kind == "base" and "unsigned" not in name and "bool" not in name)
            if kind == "pointer":
                return f"{v & 0xFFFFFFFFFFFFFFFF:#018x}"
            return f"0n{v}  ({v & ((1 << (8 * size)) - 1):#x})"
        if kind == "array" and name.startswith(("char", "wchar_t")):
            if name.startswith("wchar_t"):
                text = raw.decode("utf-16-le", "replace").split("\0", 1)[0]
            else:
                text = raw.split(b"\0", 1)[0].decode("latin-1")
            return repr(text)
        return "{" + raw[:16].hex(" ") + (" ..." if size > 16 else "") + "}"

    # -- processors / threads / contexts -- #
    def _cmd_processors(self, args):
        if not self._need_kd():
            return
        kd = self.kd
        self._ensure_context()
        if kd.user_target:
            self._cmd_user_threads(args)
            return
        n = kd.processor_count()
        if args:
            cpu = int(args[0].rstrip("s"), 0)
            if not 0 <= cpu < n:
                print(f"[!] processeur {cpu} hors plage (0..{n - 1})")
                return
            regs, label = kd.processor_context(cpu)
            th = kd.current_thread(cpu)
            kd.cpu = cpu
            pid = kd.pid_of_eprocess(kd.thread_info(th)["process"])
            self._set_context(regs, set(regs), label, th, pid)
            return
        rows = []
        for cpu in range(n):
            try:
                th = kd.current_thread(cpu)
                info = kd.thread_info(th)
                pid = kd.pid_of_eprocess(info["process"])
                proc = self.vmm.process(pid) if pid is not None else None
                regs, _l = kd.processor_context(cpu)
                sym = kd.symbolize(regs["rip"], pid)[0] if regs["rip"] else "(contexte non sauvegardé)"
                rows.append((cpu, th, pid, proc["name"] if proc else "?", regs["rip"], sym))
            except Exception as e:
                rows.append((cpu, 0, None, f"<{e}>", 0, ""))
        if self.json:
            print(_jdump([{"cpu": r[0], "thread": r[1], "pid": r[2], "process": r[3], "rip": r[4],
                           "symbol": r[5]} for r in rows]))
            return
        print(f"{'cpu':>3}  {'thread':<18} {'pid':>6}  {'process':<16} rip")
        for cpu, th, pid, name, rip, sym in rows:
            mark = "*" if cpu == kd.cpu else " "
            print(f"{mark}{cpu:>2}  {th:#018x} {pid if pid is not None else '?':>6}  {name:<16} {sym}")

    def _cmd_user_threads(self, args):
        """~ on a user-mode minidump: its threads (as WinDbg in user mode); ~<n> switches."""
        kd = self.kd
        threads = kd.e.threads
        if args:
            i = int(args[0].rstrip("s"), 0)
            if not 0 <= i < len(threads):
                print(f"[!] thread {i} hors plage (0..{len(threads) - 1})")
                return
            self._user_thread(i)
            return
        rows = []
        for i, t in enumerate(threads):
            rip = parse_context(t["context"])["rip"]
            rows.append({"index": i, "tid": t["tid"], "teb": t["teb"], "rip": rip,
                         "symbol": kd.symbolize(rip, kd.e.pid)[0],
                         "start": kd.symbolize(t["start"], kd.e.pid)[0] if t["start"] else None})
        if self.json:
            print(_jdump(rows))
            return
        exc = kd.e.exception
        print(f"  #  {'tid':>6}  {'TEB':<18} rip / début")
        for r in rows:
            mark = "." if r["index"] == kd.cpu else ("#" if exc and exc["tid"] == r["tid"] else " ")
            start = f"   (début {r['start']})" if r["start"] else ""
            print(f"{mark}{r['index']:>3}  {r['tid']:>6x}  {r['teb']:#018x} {r['symbol']}{start}")

    def _thread_arg(self, args):
        if args:
            return self.eval_expr(args[0])
        self._ensure_context()
        return self.kd.thread or self.kd.current_thread(self.kd.cpu)

    def _cmd_thread(self, args, switch=False):
        if not self._need_kd():
            return
        kd = self.kd
        if kd.user_target:
            print("[!] minidump user-mode : pas de structures noyau (ETHREAD) ; threads avec ~ et ~<n>")
            return
        th = self._thread_arg(args)
        regs, known, label, info = kd.thread_context(th)
        pid = kd.pid_of_eprocess(info["process"])
        if switch:
            self._set_context(regs, known, f".thread {th:#x} ({label})", th, pid)
            return
        proc = self.vmm.process(pid) if pid is not None else None
        state = _THREAD_STATES[info["state"]] if info["state"] is not None and info["state"] < len(_THREAD_STATES) else info["state"]
        wait = _WAIT_REASONS[info["wait_reason"]] if info["wait_reason"] < len(_WAIT_REASONS) else info["wait_reason"]
        if self.json:
            frames = kd.stack(regs, known, pid=pid, thread=th)
            print(_jdump({**info, "state_name": state, "wait_reason_name": wait,
                          "process_name": proc["name"] if proc else None,
                          "stack": [{"rsp": f.rsp, "rip": f.rip, "symbol": f.symbol} for f in frames]}))
            return
        print(f"THREAD {th:#x}  Cid {info['pid']:#x}.{info['tid']:#x}  Teb {info['teb']:#x}  "
              f"processus {proc['name'] if proc else '?'} (pid {pid})")
        print(f"  état {state}, attente {wait}")
        print(f"  début {kd.symbolize(info['start'], pid)[0]}   Win32 {kd.symbolize(info['win32_start'], pid)[0]}")
        print(f"  pile noyau {info['stack_limit']:#x}..{info['initial_stack']:#x}  KernelStack {info['kernel_stack']:#x}"
              f"  TrapFrame {info['trap_frame']:#x}")
        print(f"  ({label})")
        frames = kd.stack(regs, known, pid=pid, thread=th)
        print(f"  {'Child-SP':<17} {'RetAddr':<17} Site d'appel")
        for i, f in enumerate(frames):
            ret = frames[i + 1].rip if i + 1 < len(frames) else 0
            print(f"  {f.rsp:016x}  {ret:016x}  {f.symbol}" + (f"  [{f.line[0]} @ {f.line[1]}]" if f.line else ""))
            if f.note == "user" and i + 1 < len(frames):
                print("      --- passage en mode utilisateur ---")
            elif f.note == "nostack":
                print("      " + "(suite de la pile user-mode absente : la source ne contient pas la mémoire utilisateur)")

    def _cmd_cxr(self, args):
        if not self._need_kd():
            return
        kd = self.kd
        if not args:
            kd.ctx = None
            kd.frames = None
            self._ensure_context()
            print(f"contexte réinitialisé : {kd.ctx_label}")
            return
        addr = self.eval_expr(args[0])
        raw = kd.read(addr, 0x4D0, self.pid)
        regs = parse_context(raw)
        self._set_context(regs, set(regs), f".cxr {addr:#x}", kd.thread)

    def _cmd_trap(self, args):
        if not self._need_kd():
            return
        if not args:
            print("usage: .trap <adresse d'une KTRAP_FRAME>")
            return
        addr = self.eval_expr(args[0])
        regs, known = self.kd.trap_frame_context(addr)
        self._set_context(regs, known | {"rip", "rsp"}, f".trap {addr:#x}", self.kd.thread)

    def _cmd_regs_context(self, args):
        kd = self.kd
        self._ensure_context()
        regs, known = kd.ctx, kd.ctx_known
        if kd.frames and kd.frame_index:
            f = kd.frames[kd.frame_index]
            regs, known = f.regs, f.known
        names = [a.lower() for a in args] if args else _CTX_REGS + ["efl"]
        if self.json:
            print(json.dumps({n: hex(regs[n]) for n in names if n in regs}))
            return
        items = [(n, regs.get(n)) for n in names]
        for i in range(0, len(items), 3):
            print("  " + "   ".join(f"{n:>3}={(v if v is not None else 0):016x}" + ("" if n in known or n in ("rip", "rsp", "efl") else "?")
                                    for n, v in items[i:i + 3]))
        print(f"  ({kd.ctx_label}{', frame %02x' % kd.frame_index if kd.frame_index else ''} ; '?' = registre non restauré)")

    # -- disassembly -- #
    def _parse_range(self, args, default_count):
        addr = None
        count = default_count
        for a in args:
            if a.lower().startswith("l") and len(a) > 1:
                count = int(a[1:], 0)          # same convention as db/dq: decimal or 0x..
            else:
                addr = self.eval_expr(a)
        return addr, count

    def _cmd_u(self, args):
        kd = self.kd
        addr, count = self._parse_range(args, 8)
        if addr is None:
            addr = getattr(self, "_u_next", None)
            if addr is None:
                if kd is None:
                    print("usage: u <adresse> [L<n>]")
                    return
                self._ensure_context()
                addr = kd.ctx["rip"]
        code = self._mem(addr, count * 15 + 16)
        dis = kd.dis if kd else X64Disassembler()
        insns = dis.decode_all(code, addr, count)
        self._print_insns(insns)
        self._u_next = insns[-1].address + insns[-1].length if insns else addr

    def _print_insns(self, insns):
        kd = self.kd
        last_sym = None
        out = []
        for ins in insns:
            if kd is not None:
                sym = kd.symbolize(ins.address, self.pid)[0]
                base = sym.split("+", 1)[0]
                if "+" not in sym or base != last_sym:
                    if base != last_sym:
                        out.append(f"{sym}:")
                    last_sym = base
            note = ""
            ref = ins.target if ins.target is not None else ins.rip_ref
            if kd is not None and ref is not None:
                target_sym = kd.symbolize(ref, self.pid)[0]
                if not target_sym.startswith("0x"):
                    note = f"  ({target_sym})"
            if self.json:
                continue
            out.append(f"{ins.address:016x} {ins.raw.hex():<20} {ins.text}{note}")
        if self.json:
            print(_jdump([{"address": i.address, "bytes": i.raw.hex(), "text": i.text, "target": i.target}
                          for i in insns]))
            return
        print("\n".join(out))

    def _cmd_uf(self, args):
        if not self._need_kd():
            return
        if not args:
            print("usage: uf <adresse|module!fonction>")
            return
        kd = self.kd
        addr = self.eval_expr(args[0])
        mod, fe = kd.function_entry(addr, self.pid)
        start = addr
        root = None
        if mod is not None and fe is not None:
            root = kd._chain_root(mod, addr - mod.base, self.pid)
            start = mod.base + root[1]
        # flow analysis from the function entry: follow conditional and unconditional
        # jumps that stay inside the same function (same .pdata root), stop at returns
        seen = {}
        work = [start]
        while work and len(seen) < 5000:
            a = work.pop()
            while a not in seen and len(seen) < 5000:
                code = kd.read(a, 16, self.pid)
                if not code:
                    break
                ins = kd.dis.decode(code, a)
                seen[a] = ins
                if ins.kind == "ret" or ins.mnemonic in ("int", "ud2", "???") and ins.operands in ("3", ""):
                    break
                if ins.kind in ("jcc", "jmp") and ins.target is not None and ins.rip_ref is None:
                    same = True
                    if mod is not None and root is not None:
                        _m2, fe2 = kd.function_entry(ins.target, self.pid)
                        r2 = kd._chain_root(mod, ins.target - mod.base, self.pid) if fe2 else None
                        same = r2 is not None and r2[1] == root[1]
                    if same:
                        work.append(ins.target)
                    if ins.kind == "jmp":
                        break
                elif ins.kind == "jmp":
                    break
                a += ins.length
        self._print_insns([seen[k] for k in sorted(seen)])

    def _cmd_ln(self, args):
        if not self._need_kd():
            return
        addr = self.eval_expr(args[0]) if args else self._ensure_context()["rip"]
        sym, line = self.kd.symbolize(addr, self.pid, with_line=True)
        self._emit(f"{addr:#018x}  {sym}" + (f"   [{line[0]} @ {line[1]}]" if line else ""),
                   {"address": addr, "symbol": sym, "file": line[0] if line else None,
                    "line": line[1] if line else None})

    # -- analysis / dumps / symbols -- #
    def _cmd_analyze(self, args):
        if not self._need_kd():
            return
        self._ensure_context()
        lines = BugcheckAnalyzer(self.kd).run(verbose="-v" in args)
        print("\n".join(lines))

    def _cmd_dotdump(self, args):
        if not self._need_kd():
            return
        if not args or args[0].lower() != "/u" or len(args) < 2:
            print("usage: .dump /u [<pid>] <fichier.dmp>   (minidump user-mode complet d'un processus)")
            return
        if self.kd.user_target:
            print("[!] la cible est déjà un minidump user-mode")
            return
        pid = int(args[1], 0) if len(args) >= 3 else self.pid
        path = args[-1]
        if pid in (4, PID_PHYSICAL):
            print("[!] choisir un processus utilisateur : .dump /u <pid> <fichier>")
            return
        r = write_user_minidump(self.kd, pid, path)
        print(f"[+] {path} : {r['threads']} threads, {r['modules']} modules, "
              f"{r['ranges']} plages, {r['bytes'] / 1048576:.1f} Mo de mémoire")
        if not r["bytes"]:
            print("[~] aucune page user-mode dans la source (dump noyau) : minidump sans mémoire, avec les "
                  "threads (registres au passage en mode noyau) et les modules (reconstruits depuis les VAD)")

    def _cmd_sympath(self, args):
        if not self._need_kd():
            return
        syms = self.kd.syms
        if args:
            syms.paths = [p for p in " ".join(args).split(";") if p]
            self._cmd_reload([])
        print("chemin de symboles : " + (";".join(syms.paths) or "(aucun)") +
              f"   cache : {syms.cache}   serveur Microsoft : {'oui' if syms.download else 'non'}")

    def _cmd_reload(self, args):
        if not self._need_kd():
            return
        kd = self.kd
        for m in kd.kernel_modules() + kd.user_modules(self.pid):
            m.pdb_tried = False
            m.pdb = None
        kd.syms._pdbs.clear()
        kd._off.clear()
        kd.frames = None
        print("[+] symboles rechargés")

    def _cmd_process_threads(self, pid, flags):
        kd = self.kd
        if kd is None or not flags & 6:
            return
        kd.pid = pid
        for th in kd.process_threads(pid):
            try:
                regs, known, label, info = kd.thread_context(th)
            except Exception as e:
                print(f"    THREAD {th:#x}  <{e}>")
                continue
            state = _THREAD_STATES[info["state"]] if info["state"] is not None and info["state"] < len(_THREAD_STATES) else info["state"]
            print(f"    THREAD {th:#x}  tid {info['tid']:#x}  {state}  début {kd.symbolize(info['win32_start'] or info['start'], pid)[0]}")
            if flags & 1:
                for f in kd.stack(regs, known, pid=pid, thread=th):
                    print(f"        {f.rsp:016x} {f.symbol}")



# --------------------------------------------------------------------------- #
#  REPL
# --------------------------------------------------------------------------- #
def _jdump(obj) -> str:
    """json.dumps with every integer rendered as a hex string. 64-bit kernel
    addresses then survive a double-based JSON reader (e.g. Qt's QJsonValue, used
    by the x64dbg HvKdFusion panel) with no precision loss. Booleans stay true/false."""
    def conv(o):
        if isinstance(o, bool):
            return o
        if isinstance(o, int):
            return hex(o)
        if isinstance(o, dict):
            return {k: conv(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [conv(v) for v in o]
        return o
    return json.dumps(conv(obj))


class Repl(DebugCommands):
    def __init__(self, vmm: Vmm, json_out: bool = False, ctl: "HvControl | None" = None,
                 kd: "KernelDebugger | None" = None):
        self.vmm = vmm
        self.pid = 4          # default context: System (kernel VA); -1 = physical
        self.json = json_out
        self.ctl = ctl        # optional live-VM control channel (hvlib)
        self.kd = kd          # kernel debugger layer: symbols, contexts, stacks (optional)

    def _emit(self, text: str, data=None):
        if self.json and data is not None:
            print(_jdump(data))
        else:
            print(text)

    def resolve(self, token: str) -> int:
        """Address expression: 0x.., hex (WinDbg default), 0n decimal, registers of the
        current context, module!symbol, combined with + and -."""
        return self.eval_expr(token)

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
                if self.ctl is not None or self.kd is None:
                    self._cmd_registers(args)
                else:
                    self._cmd_regs_context(args)
            elif cmd in ("k", "kb", "kn", "kp", "kv"):
                self._cmd_k(args, args_to_child=cmd in ("kb", "kv"))
            elif cmd == "~" or (cmd.startswith("~") and cmd[1:].rstrip("s").isdigit()):
                self._cmd_processors(([cmd[1:]] if len(cmd) > 1 else []) + args)
            elif cmd == "!thread":
                self._cmd_thread(args)
            elif cmd == ".thread":
                self._cmd_thread(args, switch=True)
            elif cmd == ".cxr":
                self._cmd_cxr(args)
            elif cmd == ".trap":
                self._cmd_trap(args)
            elif cmd == "u":
                self._cmd_u(args)
            elif cmd == "uf":
                self._cmd_uf(args)
            elif cmd == "ln":
                self._cmd_ln(args)
            elif cmd == ".frame":
                self._cmd_frame(args)
            elif cmd == "dv":
                self._cmd_dv(args)
            elif cmd == "!analyze":
                self._cmd_analyze(args)
            elif cmd == ".dump":
                self._cmd_dotdump(args)
            elif cmd == ".sympath":
                self._cmd_sympath(args)
            elif cmd == ".reload":
                self._cmd_reload(args)
            else:
                print(f"commande inconnue: {cmd}  (help)")
        except (VmmError, DbgError, PdbError, DumpError) as e:
            print(f"[!] {e}")
        except ValueError as e:
            print(f"[!] argument invalide: {e}")
        except OSError as e:
            print(f"[!] {e}")
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
            print(_jdump(rows))
            return
        print(f"{'PID':>6} {'PPID':>6}  {'EPROCESS':>18}  {'PEB':>18}  Name")
        print("-" * 70)
        for p in rows:
            print(f"{p['pid']:>6} {p['ppid']:>6}  {p['eprocess']:#018x}  "
                  f"{p['peb']:#018x}  {p['name']}{'  (Wow64)' if p['wow64'] else ''}")

    def _cmd_process(self, args):
        pid = self._pid_arg(args)
        flags = int(args[1], 16) if len(args) >= 2 else 0
        p = self.vmm.process(pid)
        if not p:
            print(f"[!] pid {pid} introuvable")
            return
        if self.json:
            print(_jdump(p))
            return
        print(f"  PID {p['pid']}  PPID {p['ppid']}  '{p['name_long'] or p['name']}'")
        print(f"  EPROCESS {p['eprocess']:#x}   PEB {p['peb']:#x}   DTB {p['dtb']:#x}")
        print(f"  session {p['session']}  integrité {p['integrity']}  wow64 {p['wow64']}")
        print(f"  SID {p['sid']}")
        # !process <pid> 2|4 : threads ; 7 : threads + piles
        self._cmd_process_threads(pid, flags)

    def _cmd_lm(self, args):
        pid = self._pid_arg(args)
        try:
            mods = self.vmm.modules(pid)
        except VmmError:
            mods = []
        rebuilt = False
        if not mods and self.kd is not None and pid not in (4, -1, PID_PHYSICAL):
            # no loader list in memory (kernel dump): the images mapped in the VADs
            mods = [{"base": m.base, "size": m.size, "name": m.name, "path": m.path,
                     "timestamp": m.timestamp, "checksum": m.checksum}
                    for m in self.kd.user_modules(pid)]
            rebuilt = bool(mods)
        if self.json:
            print(_jdump(mods))
            return
        print(f"{'base':>18}  {'size':>9}  module   (pid {pid})")
        print("-" * 60)
        for m in mods:
            print(f"{m['base']:#018x}  {m['size']:>9}  {m['name']}")
        if rebuilt:
            print("(liste reconstruite depuis les VAD : PEB / liste du loader absents de la source)")

    def _cmd_x(self, args):
        if not args or "!" not in args[0]:
            print("usage: x <module>!<symbole>   (ex. x nt!PsInitialSystemProcess)")
            return
        mod, sym = args[0].split("!", 1)
        try:
            va = self.vmm.symbol(mod, sym)
        except VmmError:
            if self.kd is None:
                raise
            self.kd.pid = self.pid
            va = self.kd.sym(args[0])       # wineye's own PDB reader (symbol server)
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
        data = self._mem(addr, size)
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
        data = self._mem(start, length)
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
        pdb = None
        if self.kd is not None:
            self.kd.pid = self.pid
            m = self.kd.module_by_name(mod)
            pdb = self.kd.pdb(m) if m is not None else None
        if len(args) == 1:
            try:
                size = self.vmm.type_size(mod, typ)
            except VmmError:
                ti = pdb.find_type(typ) if pdb else None
                if ti is None:
                    raise
                size = pdb.type_size(ti)
            self._emit(f"{mod}!{typ}  taille {size:#x} ({size})",
                       {"type": f"{mod}!{typ}", "size": size})
            return
        child = args[1]
        try:
            off = self.vmm.type_child_offset(mod, typ, child)
        except VmmError:
            r = pdb.field_offset(typ, child) if pdb else None
            if r is None:
                raise
            off = r[0]
        line = f"+{off:#06x}  {typ}.{child}"
        data = {"type": f"{mod}!{typ}", "child": child, "offset": off}
        if len(args) >= 3:   # read the field value at a base address
            base = self.resolve(args[2])
            raw = self._mem(base + off, 8)
            val = int.from_bytes(raw, "little")
            line += f"  @ {base + off:#x} = {val:#x}"
            data["address"] = base + off
            data["value"] = val
        self._emit(line, data)

    def _cmd_handles(self, args):
        pid = self._pid_arg(args)
        hs = self.vmm.handles(pid)
        if self.json:
            print(_jdump(hs))
            return
        print(f"{'handle':>8}  {'object':>18}  {'type':<16} detail   (pid {pid})")
        print("-" * 70)
        for x in hs:
            print(f"{x['handle']:#08x}  {x['object']:#018x}  {x['type']:<16} {x['text']}")

    def _cmd_vad(self, args):
        pid = self._pid_arg(args)
        vs = self.vmm.vads(pid)
        if self.json:
            print(_jdump(vs))
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
#  Server backends (MCP / DAP / gdbserver) - the modes ntoseye has and the REPL
#  lacked. All read-only passive memory (VP registers only on a live VM); no run
#  control, same limit as the REPL. Pure stdlib (no pip), reusing the Vmm class.
# --------------------------------------------------------------------------- #
class WineyeOps:
    """Thin facade over Vmm (+ optional HvControl) shared by the server modes."""
    DEFAULT_PID = 4   # System: kernel-VA context

    def __init__(self, vmm: Vmm, ctl: "HvControl | None" = None, kd: "KernelDebugger | None" = None):
        self.vmm = vmm
        self.ctl = ctl
        self.kd = kd

    def _need_kd(self):
        if self.kd is None:
            raise VmmError("couche de débogage indisponible (symboles du noyau)")
        return self.kd

    def stack(self, thread=None):
        """Call stack of a thread (ETHREAD address) or of the current processor."""
        kd = self._need_kd()
        if kd.user_target:
            # user-mode minidump: thread index (default: the exception's thread, else 0)
            e = kd.e
            exc = e.exception
            if thread is not None:
                i = self._int(thread)
            else:
                i = next((n for n, t in enumerate(e.threads) if exc and t["tid"] == exc["tid"]), 0)
            t = e.threads[i]
            raw = exc["context"] if exc and exc["tid"] == t["tid"] and len(exc["context"]) >= 0x100 else t["context"]
            regs = parse_context(raw)
            kd.pid = e.pid
            return [{"index": f.index, "rsp": f.rsp, "rip": f.rip, "symbol": f.symbol,
                     "file": f.line[0] if f.line else None, "line": f.line[1] if f.line else None,
                     "transition": f.note} for f in kd.stack(regs, set(regs), pid=e.pid)]
        if thread is not None:
            th = self._int(thread)
            regs, known, _label, info = kd.thread_context(th)
            pid = kd.pid_of_eprocess(info["process"]) or 4
        else:
            cpu = kd._crash_cpu() if kd.dump is not None else 0
            regs, _label = kd.processor_context(cpu)
            known = set(regs)
            th = kd.current_thread(cpu)
            pid = kd.pid_of_eprocess(kd.thread_info(th)["process"]) or 4
        kd.pid = pid
        return [{"index": f.index, "rsp": f.rsp, "rip": f.rip, "symbol": f.symbol,
                 "file": f.line[0] if f.line else None, "line": f.line[1] if f.line else None,
                 "transition": f.note} for f in kd.stack(regs, known, pid=pid, thread=th)]

    def analyze(self):
        kd = self._need_kd()
        return "\n".join(BugcheckAnalyzer(kd).run())

    def disassemble(self, pid, addr, count=16):
        kd = self._need_kd()
        pid = self._int(pid, self.DEFAULT_PID)
        kd.pid = pid
        a = self._int(addr)
        code = kd.read(a, self._int(count, 16) * 15 + 16, pid) or self.vmm.read(pid, a, 16)
        return [{"address": i.address, "bytes": i.raw.hex(), "text": i.text,
                 "symbol": kd.symbolize(i.address, pid)[0]}
                for i in kd.dis.decode_all(code, a, self._int(count, 16))]

    @staticmethod
    def _int(v, default=None):
        if v is None:
            return default
        return v if isinstance(v, int) else int(str(v), 0)

    def processes(self):
        out = []
        for pid in sorted(self.vmm.pids()):
            p = self.vmm.process(pid)
            if p:
                out.append(p)
        return out

    def process(self, pid):
        return self.vmm.process(self._int(pid, self.DEFAULT_PID))

    def modules(self, pid):
        return self.vmm.modules(self._int(pid, self.DEFAULT_PID))

    def handles(self, pid):
        return self.vmm.handles(self._int(pid, self.DEFAULT_PID))

    def vads(self, pid):
        return self.vmm.vads(self._int(pid, self.DEFAULT_PID))

    def symbol(self, module, name):
        return self.vmm.symbol(module, name)

    def read(self, pid, addr, size):
        return self.vmm.read(self._int(pid, self.DEFAULT_PID), self._int(addr), self._int(size))

    def registers(self):
        if not self.ctl:
            raise VmmError("registres indisponibles : cible passive "
                           "(VM Hyper-V live + --vm <N> requis).")
        return {n: self.ctl.reg(HV_REG[n]) for n in _REG_ORDER}


class McpServer:
    """Minimal MCP server (JSON-RPC 2.0 over stdio, newline-delimited messages).
    Exposes wineye's passive-memory views as tools for an AI/agent client."""
    PROTOCOL = "2024-11-05"

    def __init__(self, ops: WineyeOps):
        self.ops = ops

    def _tools(self):
        pid = {"type": "string", "description": "PID (decimal or 0x..); default: System (4)"}
        return [
            {"name": "list_processes",
             "description": "List processes (pid, ppid, name, eprocess, peb).",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "process_info", "description": "Details for one process.",
             "inputSchema": {"type": "object", "properties": {"pid": pid}}},
            {"name": "list_modules", "description": "Loaded modules of a process.",
             "inputSchema": {"type": "object", "properties": {"pid": pid}}},
            {"name": "list_handles", "description": "Handles of a process.",
             "inputSchema": {"type": "object", "properties": {"pid": pid}}},
            {"name": "list_vads", "description": "Virtual address descriptors of a process.",
             "inputSchema": {"type": "object", "properties": {"pid": pid}}},
            {"name": "read_memory", "description": "Read memory; returns hex bytes.",
             "inputSchema": {"type": "object", "properties": {
                 "address": {"type": "string", "description": "0x.. or decimal"},
                 "size": {"type": "string", "description": "byte count"}, "pid": pid},
                 "required": ["address", "size"]}},
            {"name": "resolve_symbol",
             "description": "Resolve module!symbol to an address via the PDB.",
             "inputSchema": {"type": "object", "properties": {
                 "module": {"type": "string"}, "symbol": {"type": "string"}},
                 "required": ["module", "symbol"]}},
            {"name": "registers",
             "description": "VP0 registers (live Hyper-V VM only).",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "stack",
             "description": "Call stack (kernel, then user mode across the system call) of a thread "
                            "(ETHREAD address) or of the current processor; symbols from the PDBs.",
             "inputSchema": {"type": "object", "properties": {"thread": {"type": "string"}}}},
            {"name": "analyze",
             "description": "Bugcheck analysis (!analyze -v): stop code, arguments, faulting "
                            "instruction, module and stack.",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "disassemble",
             "description": "Disassemble x86-64 code at an address (Intel syntax, with symbols).",
             "inputSchema": {"type": "object", "properties": {
                 "pid": {"type": "string"}, "address": {"type": "string"}, "count": {"type": "integer"}},
                 "required": ["address"]}},
        ]

    def _call(self, name, a):
        o = self.ops
        if name == "list_processes":
            return o.processes()
        if name == "process_info":
            return o.process(a.get("pid"))
        if name == "list_modules":
            return o.modules(a.get("pid"))
        if name == "list_handles":
            return o.handles(a.get("pid"))
        if name == "list_vads":
            return o.vads(a.get("pid"))
        if name == "read_memory":
            data = o.read(a.get("pid"), a["address"], a["size"])
            return {"address": a["address"], "size": len(data), "hex": data.hex()}
        if name == "resolve_symbol":
            return {"address": hex(o.symbol(a["module"], a["symbol"]))}
        if name == "registers":
            return {k: hex(v) for k, v in o.registers().items()}
        if name == "stack":
            return [{**f, "rsp": hex(f["rsp"]), "rip": hex(f["rip"])} for f in o.stack(a.get("thread"))]
        if name == "analyze":
            return {"report": o.analyze()}
        if name == "disassemble":
            return [{**i, "address": hex(i["address"])}
                    for i in o.disassemble(a.get("pid"), a["address"], a.get("count", 16))]
        raise VmmError(f"outil inconnu : {name}")

    def serve(self):
        out = sys.stdout
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid, method = msg.get("id"), msg.get("method")
            if method == "initialize":
                resp = {"result": {"protocolVersion": self.PROTOCOL,
                                   "capabilities": {"tools": {"listChanged": False}},
                                   "serverInfo": {"name": "wineye", "version": "1.0"}}}
            elif method in ("notifications/initialized", "initialized", "notifications/cancelled"):
                continue
            elif method == "ping":
                resp = {"result": {}}
            elif method == "tools/list":
                resp = {"result": {"tools": self._tools()}}
            elif method == "tools/call":
                params = msg.get("params", {})
                try:
                    data = self._call(params.get("name"), params.get("arguments") or {})
                    resp = {"result": {"content": [{"type": "text", "text": _jdump(data)}]}}
                except Exception as e:
                    resp = {"result": {"content": [{"type": "text", "text": f"error: {e}"}],
                                       "isError": True}}
            elif mid is None:
                continue   # unknown notification: ignore
            else:
                resp = {"error": {"code": -32601, "message": f"method not found: {method}"}}
            resp.update({"jsonrpc": "2.0", "id": mid})
            out.write(json.dumps(resp) + "\n")
            out.flush()


class GdbServer:
    """Read-only GDB Remote Serial Protocol server (TCP). Serves memory (m) in the
    kernel context, and VP registers on a live VM. No run control: continue/step
    just report 'stopped' (hvlib delivers no trap events)."""
    # i386:x86-64 'g' packet order
    REG64 = ["rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp",
             "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15", "rip"]

    def __init__(self, ops: WineyeOps):
        self.ops = ops

    @staticmethod
    def _hexle(value, nbytes):
        return value.to_bytes(nbytes, "little").hex()

    @staticmethod
    def _checksum(body: bytes) -> int:
        return sum(body) & 0xFF

    def _send(self, conn, payload: str):
        body = payload.encode()
        conn.sendall(b"$" + body + b"#" + f"{self._checksum(body):02x}".encode())

    def serve(self, host, port):
        import socket
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(1)
        print(f"[*] gdbserver : écoute sur {host}:{port}  "
              f"(connecte GDB : target remote {host}:{port})", file=sys.stderr)
        while True:
            conn, addr = srv.accept()
            print(f"[+] GDB connecté : {addr}", file=sys.stderr)
            try:
                self._session(conn)
            except (ConnectionError, OSError):
                pass
            finally:
                conn.close()
            print("[~] GDB déconnecté (en attente d'une nouvelle connexion)", file=sys.stderr)

    def _session(self, conn):
        buf = b""
        noack = False
        while True:
            data = conn.recv(4096)
            if not data:
                return
            buf += data
            while buf:
                c = buf[:1]
                if c in (b"+", b"-"):
                    buf = buf[1:]
                    continue
                if c == b"\x03":            # Ctrl-C interrupt
                    buf = buf[1:]
                    self._send(conn, "S05")
                    continue
                if c != b"$":
                    buf = buf[1:]
                    continue
                end = buf.find(b"#")
                if end == -1 or len(buf) < end + 3:
                    break                   # incomplete packet, wait for more
                body = buf[1:end]
                buf = buf[end + 3:]
                if not noack:
                    conn.sendall(b"+")
                reply = self._handle(body.decode(errors="replace"))
                if reply is not None:
                    self._send(conn, reply)
                if body == b"QStartNoAckMode":
                    noack = True

    def _handle(self, p):
        if p == "?":
            return "S05"
        if p.startswith("qSupported"):
            return "PacketSize=4000;QStartNoAckMode+"
        if p == "QStartNoAckMode":
            return "OK"
        if p == "qAttached":
            return "1"
        if p == "qC":
            return "QC1"
        if p.startswith("qfThreadInfo"):
            return "m1"
        if p.startswith("qsThreadInfo"):
            return "l"
        if p.startswith(("H", "qSymbol")):
            return "OK"
        if p == "g":
            return self._read_regs()
        if p.startswith("p"):
            try:
                return self._read_one_reg(int(p[1:], 16))
            except ValueError:
                return "E01"
        if p.startswith("m"):
            return self._read_mem(p[1:])
        if p.startswith(("M", "X", "G", "P")):
            return "E01"                    # read-only
        if p.startswith(("c", "C", "s", "S")):
            return "S05"                    # no run control: report stopped
        if p in ("D", "k"):
            return "OK"
        return ""                           # unsupported -> empty

    def _regs(self):
        try:
            return self.ops.registers()
        except Exception:
            return {}

    def _read_regs(self):
        regs = self._regs()
        parts = [self._hexle(regs[n], 8) if n in regs else "xx" * 8 for n in self.REG64]
        parts.append(self._hexle(regs["rflags"], 4) if "rflags" in regs else "xx" * 4)
        parts += ["xx" * 4] * 6             # cs ss ds es fs gs: unavailable
        return "".join(parts)

    def _read_one_reg(self, n):
        regs = self._regs()
        if 0 <= n < len(self.REG64):
            name = self.REG64[n]
            return self._hexle(regs[name], 8) if name in regs else "xx" * 8
        if n == 17:
            return self._hexle(regs["rflags"], 4) if "rflags" in regs else "xx" * 4
        if 18 <= n <= 23:
            return "xx" * 4
        return "xx" * 8

    def _read_mem(self, spec):
        try:
            a, length = spec.split(",")
            data = self.ops.read(None, int(a, 16), int(length, 16))
            return data.hex() if data else "E00"
        except Exception:
            return "E01"


class DapServer:
    """Minimal read-only Debug Adapter Protocol adapter (stdio or TCP). Exposes the
    target's modules and memory (readMemory, evaluate module!symbol); no run control."""
    def __init__(self, ops: WineyeOps):
        self.ops = ops
        self.seq = 0

    def serve(self, rfile, wfile):
        self.rfile, self.wfile = rfile, wfile
        while True:
            msg = self._read()
            if msg is None:
                break
            if msg.get("type") == "request":
                if not self._dispatch(msg):
                    break

    def _read(self):
        headers = {}
        while True:
            line = self.rfile.readline()
            if not line:
                return None
            line = line.decode("ascii", "replace").strip()
            if line == "":
                break
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        n = int(headers.get("content-length", 0))
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _write(self, obj):
        data = json.dumps(obj).encode("utf-8")
        self.wfile.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii"))
        self.wfile.write(data)
        self.wfile.flush()

    def _event(self, event, body=None):
        self.seq += 1
        self._write({"seq": self.seq, "type": "event", "event": event, "body": body or {}})

    def _respond(self, req, body=None, success=True, message=None):
        self.seq += 1
        r = {"seq": self.seq, "type": "response", "request_seq": req["seq"],
             "success": success, "command": req["command"]}
        if message:
            r["message"] = message
        if body is not None:
            r["body"] = body
        self._write(r)

    def _dispatch(self, req):
        cmd = req.get("command")
        a = req.get("arguments") or {}
        try:
            if cmd == "initialize":
                self._respond(req, {"supportsReadMemoryRequest": True,
                                    "supportsModulesRequest": True,
                                    "supportsEvaluateForHovers": True})
                self._event("initialized")
            elif cmd in ("attach", "launch"):
                self._respond(req)
                self._event("stopped", {"reason": "entry", "threadId": 1,
                                        "allThreadsStopped": True})
            elif cmd == "configurationDone":
                self._respond(req)
            elif cmd == "threads":
                self._respond(req, {"threads": [{"id": 1, "name": "target (passive memory)"}]})
            elif cmd == "stackTrace":
                frames = []
                if getattr(self.ops, "kd", None) is not None:
                    try:
                        for f in self.ops.stack():
                            fr = {"id": f["index"] + 1, "name": f["symbol"] or hex(f["rip"]),
                                  "line": f["line"] or 0, "column": 0,
                                  "instructionPointerReference": hex(f["rip"])}
                            if f["file"]:
                                fr["source"] = {"name": f["file"].rsplit("\\", 1)[-1], "path": f["file"]}
                            frames.append(fr)
                    except Exception:
                        frames = []
                self._respond(req, {"stackFrames": frames, "totalFrames": len(frames)})
            elif cmd == "scopes":
                self._respond(req, {"scopes": []})
            elif cmd == "modules":
                mods = self.ops.modules(None)
                out = [{"id": m["base"], "name": m["name"], "path": m["path"],
                        "addressRange": hex(m["base"])} for m in mods]
                self._respond(req, {"modules": out, "totalModules": len(out)})
            elif cmd == "readMemory":
                import base64
                addr = int(str(a.get("memoryReference", "0")), 0) + int(a.get("offset", 0))
                count = int(a.get("count", 0))
                try:
                    data = self.ops.read(None, addr, count)
                    self._respond(req, {"address": hex(addr),
                                        "data": base64.b64encode(data).decode()})
                except Exception:
                    self._respond(req, {"address": hex(addr), "data": "",
                                        "unreadableBytes": count})
            elif cmd == "writeMemory":
                self._respond(req, success=False, message="mémoire passive : lecture seule")
            elif cmd == "evaluate":
                self._respond(req, {"result": self._evaluate((a.get("expression") or "").strip()),
                                    "variablesReference": 0})
            elif cmd == "continue":
                self._respond(req, {"allThreadsContinued": True})
                self._event("stopped", {"reason": "pause", "threadId": 1,
                                        "allThreadsStopped": True})
            elif cmd in ("next", "stepIn", "stepOut", "pause"):
                self._respond(req)
                self._event("stopped", {"reason": "step", "threadId": 1,
                                        "allThreadsStopped": True})
            elif cmd in ("disconnect", "terminate"):
                self._respond(req)
                return False
            else:
                self._respond(req, success=False, message=f"non supporté : {cmd}")
        except Exception as e:
            self._respond(req, success=False, message=str(e))
        return True

    def _evaluate(self, expr):
        if "!" in expr and " " not in expr:
            mod, sym = expr.split("!", 1)
            return hex(self.ops.symbol(mod, sym))
        if expr.lower().startswith("0x") or expr.isdigit():
            return self.ops.read(None, int(expr, 0), 16).hex()
        return "(expression non supportée : module!symbole ou adresse)"


def _parse_hostport(s, default_host, default_port):
    if not s:
        return default_host, default_port
    if ":" in s:
        host, port = s.rsplit(":", 1)
        return (host or default_host), int(port)
    return default_host, int(s)


def _serve_dap(ops, listen):
    if listen:
        import socket
        host, port = _parse_hostport(listen, "127.0.0.1", 4711)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(1)
        print(f"[*] DAP : écoute sur {host}:{port}", file=sys.stderr)
        conn, addr = srv.accept()
        print(f"[+] DAP client : {addr}", file=sys.stderr)
        f = conn.makefile("rwb")
        try:
            DapServer(ops).serve(f, f)
        finally:
            conn.close()
    else:
        DapServer(ops).serve(sys.stdin.buffer, sys.stdout.buffer)


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


def _is_kernel_dump(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(8) == b"PAGEDU64"
    except OSError:
        return False


def _is_user_minidump(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"MDMP"
    except OSError:
        return False


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
    p.add_argument("--serve", choices=["mcp", "dap", "gdb"],
                   help="lance un backend serveur au lieu de la REPL "
                        "(mcp: JSON-RPC stdio ; dap: Debug Adapter Protocol ; gdb: GDB remote)")
    p.add_argument("--listen", help="host:port pour --serve gdb (défaut 127.0.0.1:2159) "
                                    "ou dap en TCP (défaut: stdio)")
    p.add_argument("--engine", choices=["auto", "vmm", "native"], default="auto",
                   help="moteur mémoire : vmm.dll (MemProcFS), natif (dumps noyau .dmp, sans "
                        "MemProcFS), ou auto (vmm.dll, sinon natif pour un .dmp)")
    p.add_argument("--sympath", action="append",
                   help="dossier(s) de PDB privés, séparés par ';' (répétable)")
    p.add_argument("--no-symbol-download", action="store_true",
                   help="ne pas télécharger les symboles depuis le serveur Microsoft")
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # never die on an unprintable character
        except Exception:
            pass

    # In a server mode the stdout carries the protocol (MCP/DAP over stdio), so the
    # startup banner must go to stderr to keep that stream clean.
    log = sys.stderr if args.serve else sys.stdout
    sympath = [x for s in (args.sympath or []) for x in s.split(";") if x]
    store = SymbolStore(sympath=sympath, download=not args.no_symbol_download,
                        log=lambda m: print(m, file=sys.stderr))
    is_kdump = bool(args.dump) and _is_kernel_dump(args.dump)
    is_udump = bool(args.dump) and _is_user_minidump(args.dump)
    vmm = None
    if is_udump:
        # user-mode minidump: MemProcFS does not read them, wineye does
        try:
            vmm = MinidumpEngine(args.dump, error_cls=VmmError)
        except (VmmError, struct.error) as e:
            raise SystemExit(f"[!] {e}")
        print(f"[*] minidump user-mode : {args.dump}", file=log)
        print(f"[*] {vmm.process(vmm.pid)['name']} (pid {vmm.pid}), Windows build {vmm.build}, "
              f"{len(vmm.threads)} threads, {len(vmm.modules(vmm.pid))} modules"
              + (f", exception {vmm.exception['code']:08x}" if vmm.exception else ""), file=log)
    elif args.engine in ("auto", "vmm"):
        try:
            vmm_dll = find_vmm_dll(args.vmm)
            device = build_device_args(args)
            print(f"[*] vmm.dll: {vmm_dll}", file=log)
            print(f"[*] cible  : {' '.join(device)}", file=log)
            vmm = Vmm(vmm_dll, device + ["-waitinitialize"])
        except (SystemExit, VmmError) as e:
            if args.engine == "vmm" or not is_kdump:
                raise SystemExit(str(e) if str(e).startswith("[") else f"[!] {e}")
            print(f"[~] vmm.dll indisponible ({e}) : moteur natif", file=log)
    dump = None
    if vmm is None:
        if not is_kdump:
            raise SystemExit("[!] le moteur natif ne lit que les dumps noyau Windows x64 (.dmp PAGEDU64)")
        print(f"[*] moteur natif : {args.dump}", file=log)
        try:
            vmm = NativeEngine(args.dump, store, error_cls=VmmError)
        except (DumpError, VmmError, PdbError) as e:
            raise SystemExit(f"[!] {e}")
        dump = vmm.dump
        print(f"[*] dump {dump.type_name}, Windows build {dump.minor}, {dump.processors} processeur(s)", file=log)
    elif is_kdump:
        dump = CrashDump(args.dump)       # header (bugcheck, context) for the debugger layer
    kd = KernelDebugger(vmm, store, X64Disassembler(), dump=dump, log=lambda m: print(m, file=sys.stderr))
    ctl = None
    if args.vm is not None:
        try:
            ctl = HvControl(args.vm)
            print(f"[*] contrôle: canal hvlib lié à la VM {args.vm} (freeze/resume/r)", file=log)
        except Exception as e:
            print(f"[~] canal de contrôle indisponible: {e}", file=log)

    if args.serve:
        ops = WineyeOps(vmm, ctl, kd)
        print(f"[*] mode serveur: {args.serve}", file=log)
        try:
            if args.serve == "mcp":
                McpServer(ops).serve()
            elif args.serve == "gdb":
                host, port = _parse_hostport(args.listen, "127.0.0.1", 2159)
                GdbServer(ops).serve(host, port)
            else:  # dap
                _serve_dap(ops, args.listen)
        except KeyboardInterrupt:
            pass
        finally:
            vmm.close()
            if ctl:
                ctl.close()
        return

    repl = Repl(vmm, json_out=args.json, ctl=ctl, kd=kd)
    if is_udump:
        repl.pid = kd.pid = vmm.pid
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
