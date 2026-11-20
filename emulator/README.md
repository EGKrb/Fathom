# Fathom emulator (dumpulator)

Run code of a Windows process **in an emulator**, from a minidump: the memory, modules and thread of
the process are loaded in [Unicorn](https://www.unicorn-engine.org/), and what the code asks of
Windows — the **system calls** (memory, files, registry, events, sections...) and the **exceptions**
(SEH, VEH, unhandled exception filter) — is implemented in Python. A function of the process can be
called with arguments of your choice, or its thread run on; the real process is never touched.

It is [dumpulator](https://github.com/mrexodia/dumpulator) by Duncan Ogilvie (mrexodia, the author
of x64dbg), vendored here with the changes listed below, plus `fathom_emu.py`: a command line that
Fathom's `emu.*` commands and pybridge use, and that records the emulated code in x64dbg's trace
format.

## In Fathom

The ExtraCmds plugin saves the debuggee to a minidump when needed (kept until the debuggee runs
again) and runs the emulator on it; see the "Emulator" section of the [README](../README.md):

```
emu.call ntdll.RtlComputeCrc32, 0, str:hello, 5     // $result = 0x3610A686
emu.call mymodule.decrypt, buf:256, 0x140017000     // the buffer is shown after the call
emu.trace on                                        // the next emulations open in Time Travel
emu.run 0, .10000                                   // the current thread, 10,000 instructions on
emu.script "C:\scripts\decrypt.py", 0x140001000     // dumpulator's API from Python
emu.load "C:\dumps\other.dmp"                       // any minidump, even without debugging
```

From pybridge: `dbg.emulate_call("kernel32.lstrlenW", "wstr:text")`, `dbg.emulate_run(...)`, and
`dbg.emulator()`, which returns a `Dumpulator` of the debuggee in the client's Python process.

## Command line

```
python fathom_emu.py info   app.dmp
python fathom_emu.py call   app.dmp 0x140001000 0x1234 str:hello buf:256
python fathom_emu.py call   app.dmp kernel32!lstrlenA str:hello --trace out.trace64
python fathom_emu.py run    app.dmp --end 0x140001234 --count 100000 --syscalls
python fathom_emu.py script app.dmp examples\crc_strings.py ntdll!RtlComputeCrc32
python fathom_emu.py setup                         # pip install -r requirements.txt
```

| Option | |
|---|---|
| `call DUMP ADDRESS [ARG ...]` | calls a function. `ADDRESS`: `0x...` or `module!export`. Arguments: integers, `str:TEXT` (ANSI string allocated in the emulator), `wstr:TEXT` (UTF-16), `buf:SIZE` (zeroed buffer, shown after the call), `bytes:HEX` |
| `run DUMP [--begin A] [--end A]` | runs the dumped thread from its instruction pointer (or `--begin`) |
| `script DUMP FILE [ARG ...]` | runs a Python script with `dp` (the `Dumpulator`), `args` and `result` |
| `--thread TID` | the thread to emulate (default: the thread of the dump's exception record, else the first) |
| `--count N` | stops after N instructions |
| `--trace FILE` | records the instructions to an x64dbg trace (`.trace64`, or `.trace32` for a 32-bit process) |
| `--syscalls` | shows the system calls and exceptions |
| `--text-trace` | also writes dumpulator's own text trace (`<dump>.trace`) |
| `--result FILE` | writes the result as JSON (return value, final address, buffers, trace, exit code) |

The dump must contain the full memory of the process: Fathom's `minidump` command (or `.save
file.dmp`), Task Manager's "Create dump file", `procdump -ma`. Python 3.9 or later with the packages
of `requirements.txt` (unicorn 2.1, capstone 5, pefile); tested with Python 3.14.

## Traces

`TraceWriter` (in `fathom_emu.py`) hooks every emulated instruction and writes a block in the format
of x64dbg's recorder ([`docs/developers/tracefile.md`](../docs/developers/tracefile.md)): the
registers before the instruction (x64dbg's `REGDUMP`: general, segment, debug, MXCSR, XMM and YMM
registers and the decoded flags, those that changed since the previous block and all of them every
512 instructions; the x87 registers stay at zero) and the memory the instruction read or wrote, one pointer-sized word per access, read
before and after it. So `TraceExport` converts the trace to JSON, CSV, Tenet, Frinet or `.ftt`, and
the Time Travel tab and x64dbg's trace view open it. The registers are read with one call to
unicorn's library per instruction: about 40,000 instructions per second while recording.

Not in the trace: instructions in the kernel half of the address space (dumpulator returns from
exceptions through a small stub there) and, for a 32-bit process, the 64-bit code; what the system
calls write to memory (they are Python code), only their result in the registers. When an
instruction faults, it is written once, followed by the exception dispatcher.

From Python:

```python
from dumpulator import Dumpulator
from fathom_emu import TraceWriter

dp = Dumpulator("app.dmp", quiet=True)
writer = TraceWriter(dp, "out.trace64")
dp.call(0x140001000, [0x1234])
print(writer.close(), "instructions")
```

## Changes to dumpulator

dumpulator (v1.0.0, commit `18a64d7`) is in `dumpulator/` and its patched copy of skelsec's
`minidump` library in `minidump/` (unchanged). Changed for unicorn 2.1 and Python 3.12+, which the
pinned versions of dumpulator (unicorn 2.0, capstone 4) no longer install on:

- `dumpulator.py`: unicorn's virtual TLB mode (`ctl_set_tlb_mode(UC_TLB_VIRTUAL)`), without which
  unicorn 2.1 cannot reach the GDT and the code stub dumpulator puts in the kernel half of the address
  space; `hook_add` with positional arguments (`arg1` became `aux1`); `ctl_request_cache` returns a
  tuple in unicorn 2.1.
- `ntprimitives.py`: the type annotations of a structure are read from its class (Python 3.14 no
  longer finds them on an instance).
- `ntsyscalls.py`: `NtOpenThreadToken` / `NtOpenThreadTokenEx` return `STATUS_NO_TOKEN` (the
  emulated thread does not impersonate) instead of stopping the emulation.
- `avx.py` (new) and its call in `dumpulator.py`: **AVX and AVX2 instructions in Python**. Unicorn
  has no AVX, but the C runtime of a process that ran on a recent CPU chose its AVX2 routines at
  start-up (`wcslen`, `memcpy`, `memchr`...): the logical, comparison, minimum / maximum, add /
  subtract, mask, test, broadcast, shuffle, align, permute, insert / extract, unpack and byte-shift
  forms on `xmm` and `ymm` registers are emulated when unicorn rejects them.
  `tests/test_avx.py` runs each of them natively and emulated on random values and compares the
  results (`python tests/test_avx.py`, needs a CPU with AVX2).

With these changes, dumpulator's own tests (its `Tests` DLL on the `HarnessMinimal` dumps, x64 and
x86: handles, files, memory) pass, as does its exception test (SEH, VEH, vectored continue handler,
unhandled exception filter) run to the exit of the process.

## Limits

- One thread. Code that waits for another thread, or a system call dumpulator does not implement,
  stops the emulation (the last system call shown with `--syscalls` names it). Calling functions
  works far better than running a whole program: the start-up of the C runtime alone makes system
  calls dumpulator does not have.
- AVX-512 instructions are not emulated (a CPU with AVX-512 makes some runtimes use them).
- An emulated function that loops forever runs until `--count`.

## Licenses

dumpulator: Boost Software License 1.0 ([`LICENSE.dumpulator`](LICENSE.dumpulator)), Copyright
Duncan Ogilvie. minidump: MIT ([`LICENSE.minidump`](LICENSE.minidump)), Copyright Tamas Jos
(skelsec). The changes to dumpulator and `avx.py`: Boost Software License 1.0 too, so that they can
go back to dumpulator. `fathom_emu.py` and the tests: GPL-3.0-or-later, like Fathom.
