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


class ApiHooks:
    def __init__(self, dp):
        self.dp = dp
        self.hooks: Dict[str, Dict[str, _Hook]] = {}     # module (lower case) -> name -> hook
        self.installed: Dict[int, str] = {}             # address -> name
        self.frames: Dict[int, List[_Frame]] = {}       # thread -> hooked calls waiting for an invoke
        self.trampoline = 0
        self.calls: Dict[str, int] = {}                 # name -> number of calls (statistics)

    # ------------------------------------------------------------------ registration
    def register(self, module: str, name: str, argcount: int, handler: Callable):
        hook = _Hook(module.lower(), name, argcount, handler)
        self.hooks.setdefault(hook.module, {})[name] = hook
        found = self.dp.modules.find(hook.module)
        if found is not None:
            self._install(found, hook)

    def module_mapped(self, module):
        """A module was mapped: install the hooks registered for it."""
        for hook in self.hooks.get(module.name.lower(), {}).values():
            self._install(module, hook)

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
