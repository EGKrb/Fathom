# Fathom addition to dumpulator: functions of DLLs replaced by Python code (API hooks), for what the
# emulator implements above the system calls: the network (ws2_32, wininet, winhttp), the windows
# (user32) and the child processes (CreateProcess). Boost Software License 1.0, like the rest of
# dumpulator (LICENSE.dumpulator).
#
# A handler receives the arguments of the function (Windows x64 convention, or stdcall in 32-bit)
# and returns its result: the function returns to its caller with it. A handler can instead return
# Invoke(function, arguments, then): the emulated function is called (a window procedure, Sleep...)
# and then(result) decides what comes next (another Invoke, or the result of the hooked function).
# A handler that returns CALL_ORIGINAL lets the original function run.

import struct
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from unicorn import UC_HOOK_CODE

CALL_ORIGINAL = object()


@dataclass
class Invoke:
    function: int
    arguments: List[int] = field(default_factory=list)
    then: Optional[Callable[[int], Any]] = None


@dataclass
class _Hook:
    module: str
    name: str
    argcount: int
    handler: Callable


@dataclass
class _Frame:
    """A hooked call in progress (it invoked emulated code)."""
    entry_sp: int       # stack pointer at the entry of the hooked function (on its return address)
    argcount: int
    then: Optional[Callable]


# The DLLs whose functions are hooked only when a module imports them or GetProcAddress asks for them:
# each hook is a code hook of unicorn, whose translation slows down with their number, and these
# DLLs have thousands of functions implemented (gdiplus_emu.py, opengl_emu.py, gl_emu.py, d3d9_emu.py,
# dxgi_emu.py, d3d10_emu.py, d3d11_emu.py, d3d12_emu.py).
LAZY_MODULES = {"gdiplus.dll", "opengl32.dll", "glu32.dll", "d3d9.dll", "d3d10.dll", "d3d10_1.dll", "d3d11.dll",
                "d3d12.dll", "dxgi.dll"}
# the DLLs whose DllMain is not run (their API is the emulator's)
SKIP_DLLMAIN = {"d3d9.dll", "d3d10.dll", "d3d10_1.dll", "d3d10core.dll", "d3d11.dll", "d3d12.dll", "d3d12core.dll",
                "dxgi.dll", "opengl32.dll", "glu32.dll"}


class ApiHooks:
    def __init__(self, dp):
        self.dp = dp
        self.hooks: Dict[str, Dict[str, _Hook]] = {}     # module (lower case) -> name -> hook
        self.installed: Dict[int, str] = {}             # address -> name
        self.frames: Dict[int, List[_Frame]] = {}       # thread -> hooked calls waiting for an invoke
        self.trampoline = 0
        self.calls: Dict[str, int] = {}                 # name -> number of calls (statistics)
        self.wanted: Optional[Dict[str, set]] = None    # lazy module -> the names imported or asked for
        self.ordinals: Dict[str, set] = {}              # lazy module -> the ordinals imported (named once mapped)

    # ------------------------------------------------------------------ registration
    def register(self, module: str, name: str, argcount: int, handler: Callable):
        hook = _Hook(module.lower(), name, argcount, handler)
        self.hooks.setdefault(hook.module, {})[name] = hook
        if hook.module in LAZY_MODULES:
            self._lazy_setup()
            if name not in self.wanted.get(hook.module, ()):
                return
        found = self.dp.modules.find(hook.module)
        if found is not None:
            self._install(found, hook)

    def _skip_entry(self, module):
        """The DLLs whose functions the emulator implements: their DllMain returns TRUE at once (their
        own initialization and teardown would work on state the emulated functions never create)."""
        name = module.name.lower()
        if name in SKIP_DLLMAIN and module.entry and module.entry != module.base:
            self.hook_address(module.entry, f"{name}!DllMain", 3, lambda dp, *args: 1)

    def module_mapped(self, module):
        """A module was mapped: install the hooks registered for it (and those it imports)."""
        self._skip_entry(module)
        if self.wanted is not None:
            self._scan_imports(module)
        name = module.name.lower()
        for ordinal in sorted(self.ordinals.get(name, ())):
            self.want_ordinal(name, ordinal)
        for hook in self.hooks.get(name, {}).values():
            if name not in LAZY_MODULES or hook.name in self.wanted.get(name, ()):
                self._install(module, hook)

    def want(self, module: str, name: str):
        """A function of a lazy module is used: its hook, if any, is installed."""
        module = module.lower()
        names = self.wanted.setdefault(module, set())
        if name in names:
            return
        names.add(name)
        hook = self.hooks.get(module, {}).get(name)
        found = self.dp.modules.find(module) if hook is not None else None
        if found is not None:
            self._install(found, hook)

    def want_ordinal(self, module: str, ordinal: int):
        """A function of a lazy module imported by ordinal (d3d12!D3D12CreateDevice is ordinal 101 in
        d3d12.lib): its name comes from the export table once the module is mapped."""
        module = module.lower()
        self.ordinals.setdefault(module, set()).add(ordinal)
        found = self.dp.modules.find(module)
        export = found.find_export(ordinal) if found is not None else None
        if export is not None and export.ordinal == ordinal and export.name:
            self.want(module, export.name)

    def _lazy_setup(self):
        if self.wanted is not None:
            return
        self.wanted = {}
        for module in list(self.dp.modules):
            self._scan_imports(module)
            self._skip_entry(module)
        kernelbase = self.dp.modules.find("kernelbase.dll")
        if kernelbase is not None:
            hook = _Hook("kernelbase.dll", "GetProcAddress", 2, self._get_proc_address)
            self.hooks.setdefault("kernelbase.dll", {})["GetProcAddress"] = hook
            self._install(kernelbase, hook)

    def _get_proc_address(self, dp, handle, name):
        """GetProcAddress of a function of a lazy module: hooked before the original runs."""
        module = self.dp.modules.find(handle & self.dp.addr_mask) if handle else None
        if module is not None and module.name.lower() in LAZY_MODULES:
            if name <= 0xFFFF:
                self.want_ordinal(module.name, name)
            else:
                text = read_astring(dp, name, 256)
                if text:
                    self.want(module.name, text)
        return CALL_ORIGINAL

    def _scan_imports(self, module):
        """The functions of the lazy modules that module imports (import and delay-import tables, read in
        the emulator's memory)."""
        dp = self.dp
        base = module.base
        try:
            nt = base + struct.unpack("<I", bytes(dp.read(base + 0x3C, 4)))[0]
            pe32_plus = struct.unpack("<H", bytes(dp.read(nt + 24, 2)))[0] == 0x20B
            directories = nt + 24 + (112 if pe32_plus else 96)
            imports, _ = struct.unpack("<2I", bytes(dp.read(directories + 8, 8)))
            delayed, _ = struct.unpack("<2I", bytes(dp.read(directories + 13 * 8, 8)))
            thunk_size = 8 if pe32_plus else 4
            tables = []
            for k in range(4096) if imports else ():
                original, _, _, name, first = struct.unpack("<5I", bytes(dp.read(base + imports + 20 * k, 20)))
                if not name and not first:
                    break
                tables.append((name, original or first))
            for k in range(4096) if delayed else ():
                attributes, name, _, _, names = struct.unpack("<5I", bytes(dp.read(base + delayed + 32 * k, 20)))
                if not name:
                    break
                if attributes & 1:
                    tables.append((name, names))
            for name, table in tables:
                dll = (read_astring(dp, base + name, 256) or "").lower()
                if dll not in LAZY_MODULES or not table:
                    continue
                for i in range(65536):
                    raw = bytes(dp.read(base + table + thunk_size * i, thunk_size))
                    value = int.from_bytes(raw, "little")
                    if not value:
                        break
                    if value >> (thunk_size * 8 - 1):
                        self.want_ordinal(dll, value & 0xFFFF)
                        continue
                    if value >= 0x80000000:
                        continue  # a bound address, not a name
                    function = read_astring(dp, base + value + 2, 256)
                    if function:
                        self.want(dll, function)
        except Exception:
            pass

    def _install(self, module, hook: _Hook):
        export = module.find_export(hook.name)
        if export is None:
            return
        if getattr(export, "forward", None):
            # a forwarder (user32!DefWindowProcW -> ntdll!NtdllDefWindowProc_W on x64): the function
            # it leads to, once its module is mapped
            try:
                export = self.dp.modules.resolve_export(*export.forward)
            except Exception:
                return
        address = export.address
        if address in self.installed:
            return
        self.installed[address] = hook.name
        self.dp._uc.hook_add(UC_HOOK_CODE, self._on_call, hook, address, address)

    def _ensure_trampoline(self):
        if not self.trampoline:
            # a page of int3: the return address of the invoked functions, hooked
            page = self.dp.allocate(0x1000, page_align=True)
            self.dp.write(page, b"\xCC" * 0x1000)
            self.trampoline = page
            self.dp._uc.hook_add(UC_HOOK_CODE, self._on_return, None, page, page)

    # ------------------------------------------------------------------ calls
    def argument(self, index: int) -> int:
        return self.dp.args[index]

    def _on_call(self, uc, address, size, hook: _Hook):
        dp = self.dp
        if uc.reg_read(_ip_register(dp)) != address:
            return
        args = [dp.args[i] for i in range(hook.argcount)]
        self.calls[hook.name] = self.calls.get(hook.name, 0) + 1
        try:
            result = hook.handler(dp, *args)
        except Exception as e:
            import traceback
            traceback.print_exc()
            dp.error(f"API hook {hook.name}: {type(e).__name__}: {e}")
            result = 0
        if result is CALL_ORIGINAL:
            return
        frame = _Frame(dp.regs.csp, hook.argcount, None)
        self._continue(frame, result)

    def _continue(self, frame: _Frame, result):
        """Either return from the hooked function with result, or invoke emulated code."""
        dp = self.dp
        if isinstance(result, Invoke):
            self._ensure_trampoline()
            frame.then = result.then
            self.frames.setdefault(dp.thread_id, []).append(frame)
            self._call(result.function, result.arguments)
            return
        self._return(frame, result)

    def _return(self, frame: _Frame, value):
        dp = self.dp
        ptr = dp.ptr_size()
        regs = dp.regs
        return_address = dp.read_ptr(frame.entry_sp)
        regs.csp = frame.entry_sp + ptr + (0 if dp._x64 else 4 * frame.argcount)
        regs.cax = int(value) & dp.addr_mask if value is not None else 0
        regs.cip = return_address
        vex = getattr(dp, "_fathom_vex", None)
        if vex is not None:     # the block after the return is translated without the hook of new blocks
            vex.scan(return_address)

    def _call(self, function: int, arguments: List[int]):
        """Call emulated code below the current stack, returning to the trampoline."""
        dp = self.dp
        regs = dp.regs
        ptr = dp.ptr_size()
        sp = (regs.csp - 0x200) & ~0xF
        if dp._x64:
            # at the entry: [rsp] return address, rsp+8 shadow space (4 slots), rsp+0x28 the 5th
            # argument...; rsp = 8 modulo 16
            stack_args = arguments[4:]
            sp -= 0x28 + 8 * len(stack_args)
            if sp % 16 != 8:
                sp -= 8
            for i, value in enumerate(stack_args):
                dp.write(sp + 0x28 + 8 * i, struct.pack("<Q", value & dp.addr_mask))
            for register, value in zip(("rcx", "rdx", "r8", "r9"), arguments[:4]):
                regs[register] = value & dp.addr_mask
            dp.write(sp, struct.pack("<Q", self.trampoline))
        else:
            for value in reversed(arguments):
                sp -= 4
                dp.write(sp, struct.pack("<I", value & 0xFFFFFFFF))
            sp -= 4
            dp.write(sp, struct.pack("<I", self.trampoline))
        regs.csp = sp
        regs.cip = function

    def _on_return(self, uc, address, size, _):
        dp = self.dp
        frames = self.frames.get(dp.thread_id)
        if not frames:
            dp.error(f"API hooks: return to the trampoline without a call in progress (thread {dp.thread_id})")
            return
        frame = frames.pop()
        value = dp.regs.cax
        result = frame.then(value) if frame.then is not None else value
        # the stack goes back to the hooked function's entry before the next step
        dp.regs.csp = frame.entry_sp
        self._continue(frame, result)


def _ip_register(dp):
    from unicorn.x86_const import UC_X86_REG_RIP
    return UC_X86_REG_RIP


# ------------------------------------------------------------------ helpers for the handlers
def read_wstring(dp, address: int, limit: int = 0x10000) -> Optional[str]:
    if not address:
        return None
    data = bytearray()
    while len(data) < limit:
        chunk = bytes(dp.read(address + len(data), 2))
        if chunk == b"\0\0":
            break
        data += chunk
    return data.decode("utf-16-le", "replace")


def read_astring(dp, address: int, limit: int = 0x10000) -> Optional[str]:
    if not address:
        return None
    data = bytearray()
    while len(data) < limit:
        byte = bytes(dp.read(address + len(data), 1))
        if byte == b"\0":
            break
        data += byte
    return data.decode("mbcs", "replace")


def read_string(dp, address: int, wide: bool) -> Optional[str]:
    return read_wstring(dp, address) if wide else read_astring(dp, address)


def write_string(dp, text: str, wide: bool) -> int:
    """A NUL-terminated string allocated in the emulator; its address."""
    data = text.encode("utf-16-le") + b"\0\0" if wide else text.encode("mbcs", "replace") + b"\0"
    address = dp.allocate(len(data))
    dp.write(address, data)
    return address


def set_last_error(dp, code: int):
    dp.write_ulong(dp.teb + (0x68 if dp._x64 else 0x34), code)


def to_signed(value: int, bits: int = 32) -> int:
    value &= (1 << bits) - 1
    return value - (1 << bits) if value >> (bits - 1) else value


# ------------------------------------------------------------------ typed arguments (floats, doubles)
def signature_slots(signature: str, x64: bool) -> int:
    """The pointer-sized stack slots of the arguments (what stdcall pops in 32-bit)."""
    if x64:
        return len(signature)
    return sum(2 if k in "dq" else 1 for k in signature)


def read_args(dp, signature: str) -> list:
    """The arguments of a function at its entry: p a pointer-sized value, i / h / b an integer of 32 /
    16 / 8 bits (unsigned: the upper bits of its register or stack slot are not part of it), f a float,
    d a double, q a 64-bit value. x64: the first four in rcx / rdx / r8 / r9 or xmm0-3 (floats), the
    others on the stack; 32-bit: on the stack, 8 bytes for d and q."""
    import struct as _struct
    from unicorn.x86_const import UC_X86_REG_XMM0
    masks = {"i": 0xFFFFFFFF, "h": 0xFFFF, "b": 0xFF}
    values = []
    if dp._x64:
        sp = dp.regs.rsp
        for i, k in enumerate(signature):
            if i < 4 and k in "fd":
                raw = dp._uc.reg_read(UC_X86_REG_XMM0 + i) & 0xFFFFFFFFFFFFFFFF
                values.append(_struct.unpack("<f", _struct.pack("<I", raw & 0xFFFFFFFF))[0] if k == "f"
                              else _struct.unpack("<d", _struct.pack("<Q", raw))[0])
            elif i < 4:
                values.append(dp.args[i] & masks.get(k, 0xFFFFFFFFFFFFFFFF))
            else:
                slot = bytes(dp.read(sp + 8 * (i + 1), 8))
                values.append(_struct.unpack("<f", slot[:4])[0] if k == "f" else
                              _struct.unpack("<d", slot)[0] if k == "d" else
                              _struct.unpack("<Q", slot)[0] & masks.get(k, 0xFFFFFFFFFFFFFFFF))
        return values
    offset = dp.regs.esp + 4
    for k in signature:
        size = 8 if k in "dq" else 4
        data = bytes(dp.read(offset, size))
        values.append(_struct.unpack("<f", data)[0] if k == "f" else _struct.unpack("<d", data)[0] if k == "d" else
                      _struct.unpack("<Q", data)[0] if k == "q" else _struct.unpack("<I", data)[0] & masks.get(k, 0xFFFFFFFF))
        offset += size
    return values


def _hook_address(self, address: int, name: str, argcount: int, handler: Callable):
    """A Python function at an address that is no export (the methods of an emulated COM object)."""
    if address in self.installed:
        return
    self.installed[address] = name
    self.dp._uc.hook_add(UC_HOOK_CODE, self._on_call, _Hook("", name, argcount, handler), address, address)


def _register_signature(self, module: str, name: str, signature: str, handler: Callable):
    """register() with typed arguments: handler(dp, *values)."""
    self.register(module, name, signature_slots(signature, self.dp._x64),
                  lambda dp, *raw: handler(dp, *read_args(dp, signature)))


ApiHooks.hook_address = _hook_address
ApiHooks.register_signature = _register_signature


# ------------------------------------------------------------------ emulated COM objects
E_NOINTERFACE, E_NOTIMPL, E_POINTER = 0x80004002, 0x80004001, 0x80004003
IID_IUNKNOWN = "00000000-0000-0000-C000-000000000046"


def read_guid(dp, address: int) -> str:
    import uuid
    return str(uuid.UUID(bytes_le=bytes(dp.read(address, 16)))).upper()


class ComObject:
    def __init__(self, impl, interface: str, address: int):
        self.impl = impl
        self.interface = interface
        self.address = address


class ComRuntime:
    """COM objects implemented in Python: each interface gets a vtable of stubs (a ret each), the call
    of a stub goes to the method of the same name of the object's implementation (a Python object).
    AddRef / Release / QueryInterface are generic unless the implementation has them; a method it
    lacks returns E_NOTIMPL and is named in the summary (not_implemented)."""

    def __init__(self, dp):
        self.dp = dp
        self.vtables = {}
        self.objects = {}           # address -> ComObject
        self.aliases = {}           # (id(impl), interface) -> address

    def _vtable(self, interface: str) -> int:
        if interface in self.vtables:
            return self.vtables[interface]
        from .graphics_tables import INTERFACES
        dp = self.dp
        methods = INTERFACES[interface]
        ptr = dp.ptr_size()
        stubs = dp.allocate(len(methods), page_align=True)
        dp.write(stubs, b"\xC3" * len(methods))
        table = dp.allocate(ptr * len(methods))
        for index, (name, signature) in enumerate(methods):
            address = stubs + index
            dp.write_ptr(table + ptr * index, address)
            argcount = 1 + signature_slots(signature, dp._x64)
            self.dp.apihooks.hook_address(address, f"{interface}::{name}", argcount,
                                          lambda dp, *raw, i=interface, n=index: self._dispatch(i, n))
        self.vtables[interface] = table
        return table

    def create(self, impl, interface: str) -> int:
        """The address of a new object (its vtable for interface) implemented by impl."""
        key = (id(impl), interface)
        if key in self.aliases:
            return self.aliases[key]
        dp = self.dp
        address = dp.allocate(4 * dp.ptr_size())
        dp.write_ptr(address, self._vtable(interface))
        self.objects[address] = ComObject(impl, interface, address)
        self.aliases[key] = address
        if not hasattr(impl, "refs"):
            impl.refs = 1
        if getattr(impl, "com_address", None) is None:
            impl.com_address = address
        return address

    def impl(self, address: int):
        obj = self.objects.get(address & self.dp.addr_mask) if address else None
        return obj.impl if obj is not None else None

    def _dispatch(self, interface: str, index: int):
        from .graphics_tables import INTERFACES, IIDS
        dp = self.dp
        name, signature = INTERFACES[interface][index]
        values = read_args(dp, "p" + signature)
        obj = self.objects.get(values[0] & dp.addr_mask)
        if obj is None:
            return E_POINTER
        handler = getattr(obj.impl, name, None)
        if handler is not None and callable(handler):
            result = handler(*values[1:])
            result = 0 if result is None else result
            if not dp._x64 and name in getattr(obj.impl, "WIDE", ()):
                dp.regs.edx = (int(result) >> 32) & 0xFFFFFFFF  # a 64-bit result: edx:eax
            return result
        if name == "AddRef":
            obj.impl.refs += 1
            return obj.impl.refs
        if name == "Release":
            obj.impl.refs = max(0, obj.impl.refs - 1)
            return obj.impl.refs
        if name == "QueryInterface":
            iid, out = read_guid(dp, values[1]), values[2]
            for candidate in [obj.interface] + list(getattr(obj.impl, "interfaces", [])):
                if iid == IID_IUNKNOWN or IIDS.get(candidate) == iid:
                    dp.write_ptr(out, self.create(obj.impl, candidate))
                    obj.impl.refs += 1
                    return 0
            dp.write_ptr(out, 0)
            return E_NOINTERFACE
        missing = dp._fathom_unimplemented
        key = f"{interface}::{name}"
        if key not in missing:
            dp.error(f"{key} is not emulated: E_NOTIMPL")
        missing[key] = missing.get(key, 0) + 1
        return E_NOTIMPL


def com_runtime(dp) -> ComRuntime:
    if getattr(dp, "com_runtime", None) is None:
        dp.com_runtime = ComRuntime(dp)
    return dp.com_runtime
