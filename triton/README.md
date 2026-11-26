# Triton plugin: symbolic execution and taint analysis

The **Triton** plugin (`Triton.dp64` / `Triton.dp32`) brings [Triton](https://github.com/JonathanSalwan/Triton), the dynamic binary analysis library by Jonathan Salwan and Quarkslab, into Fathom: **symbolic execution**, **taint analysis**, **SMT solving** with [Z3](https://github.com/Z3Prover/z3), **expression simplification and synthesis**, through `sym.*` and `taint.*` commands.

Triton runs on one of two **sources**:

- **the debuggee** (`sym.start`): Triton starts from the current thread's registers and reads the process memory as it needs it. It then executes the code itself: **the debuggee does not move** (as with the emulator's `emu.*` commands);
- **a recorded trace** (`sym.trace`, `.ftt` or x64dbg's `.trace64` / `.trace32`): each recorded instruction is replayed with the registers and memory values the trace holds. So a real execution (API calls, other threads, kernel included) is analyzed symbolically afterwards.

What for: find **the input that passes a check** (a serial, a password, a magic value), take **the other side of a branch**, see **what a value is computed from**, **simplify obfuscated arithmetic** (MBA, opaque predicates), **follow the data** of an input through the code.

## Example: a serial check

The debuggee is stopped at the entry of `check(serial)` (64-bit: the argument in `rcx`):

```
sym.start                       // a Triton session on the current state
sym.mem rcx, 8, serial          // the 8 bytes of the serial become variables serial_0..serial_7
sym.run                         // Triton runs check() to its return (the debuggee stays here)
sym.expr eax                    // the return value as a formula of serial_0..7
sym.solve eax, 1                // Z3: the serial for which check() returns 1
sym.apply                       // writes it into the debuggee's memory
rtr                             // the debuggee now runs check() natively: eax = 1
```

The log shows the solution byte by byte and as text:

```
[Triton] eax == 0x1: solution found (3 ms):
[Triton]   serial_0 (SymVar_0) [0xD91950FD30], 8 bits = 0x66
...
[Triton]   bytes at 0xD91950FD30: "f}\x05Zf\x02L\xA3"
```

A check with **one branch per character** returns at the first wrong one: `sym.branches` lists the branches that depend on the input, `sym.flip 0` solves the input that takes the other side of the first one, `sym.apply` writes it, and a new `sym.start` / `sym.run` goes one comparison further. `sym.reach address` solves the input that reaches an address through a recorded branch.

**Obfuscated code**: at the entry of a function `f(x, y)`, `sym.reg ecx, x` and `sym.reg edx, y` (or `sym.memvar esp+4, 4, x` in 32-bit), `sym.run`, then `sym.synth eax` finds a simpler expression with the same values, e.g. `(x ^ y) + 2 * (x & y)` → `(y + x) & 0xffffffff`. `sym.expr` shows the expression simplified by Z3, `sym.smt` in SMT-LIB, and `sym.slice` lists the instructions it comes from.

**Taint**: `taint.mem address, size` (or `taint.reg`), `sym.run`, then `sym.list` lists the instructions that used the tainted (or symbolic) data and `sym.mark` comments them in the disassembly (`Triton: tainted`); `sym.unmark` removes the comments.

**A recorded trace**: record with x64dbg's trace recorder (`StartTraceRecording`, `TraceIntoConditional`, or the Trace menu), then

```
sym.trace "C:\traces\app.trace64", 0       // from instruction 0 of the trace, its thread
sym.mem rcx, 8, serial                     // rcx as recorded at that instruction
sym.run ret                                // replays the recorded instructions up to the address ret
sym.solve eax, 1
```

## Commands

| Command | Effect |
|---|---|
| `sym.start` | a session on the debuggee's current thread: registers (general, flags, `xmm`/`ymm`, `mxcsr`, segment base: the TEB), memory read on demand |
| `sym.trace file[, seq[, thread]]` | a session on a trace (`.ftt`, or a `.trace32` / `.trace64` converted to `.ftt` once, in `%TEMP%\FathomTimeTravel`) from instruction `seq` (default 0) of its thread (or of `thread`) |
| `sym.stop` | ends the session |
| `sym.info` | the session: source, position, instructions executed, symbolic variables, branches, tainted data |
| `sym.reg reg[, name]` | makes a register symbolic (it keeps its concrete value) |
| `sym.mem address, size[, name]` | makes memory symbolic, one 8-bit variable per byte (`name_0`, `name_1`...) |
| `sym.memvar address, size[, name]` | makes 1, 2, 4 or 8 bytes of memory one symbolic variable (an integer argument on the stack) |
| `sym.concrete reg \| address[, size]` | drops the symbolic data of a register / memory |
| `taint.reg reg`, `taint.mem address, size` | taints a register / memory (`untaint.reg`, `untaint.mem`: the opposite) |
| `sym.step [count]` | executes `count` instructions (default 1) |
| `sym.run [end[, count]]` | runs up to the address `end`; without `end`: the debuggee source runs until the current function returns, the trace source to the end of the thread's trace. At most `count` instructions (default 10,000,000). `$result` = the address reached, `$result1` = the number of instructions |
| `sym.expr reg \| address[, size]` | the expression of a register / memory (`size` bytes, default pointer-sized), simplified by Z3; `$result` = its concrete value, `$result1` = 1 if it is symbolic |
| `sym.smt reg \| address[, size]` | the same, in SMT-LIB |
| `sym.synth reg \| address[, size]` | a simpler expression with the same values (Triton's synthesis); `$result` = 1 if found |
| `sym.slice reg \| address[, size]` | the instructions the expression comes from; `$result` = their number |
| `sym.vars` | the symbolic variables |
| `sym.branches` | the branches that depended on symbolic data: address, side taken, other side; `$result` = their number |
| `sym.flip n` | the input that takes the other side of branch `n` (the branches before it unchanged); `$result` = 1 if found |
| `sym.reach address` | the input that reaches `address` through a recorded branch |
| `sym.solve target, value[, size]` | the input for which `target` (a register, or `size` bytes at an address) equals `value` at this point of the path |
| `sym.apply` | writes the last solution into the debuggee (memory and registers made symbolic) |
| `sym.list [count]` | the last instructions that used symbolic or tainted data (default 50); `$result` = their total |
| `sym.mark` / `sym.unmark` | comments those instructions in the disassembly / removes the comments |

The arguments are x64dbg expressions, evaluated **in the state of the session**: a register is Triton's (after `sym.run`, or at the position of a trace, not the debuggee's), and `[address]` reads the session's memory. Labels (`module.function`), numbers and script variables are x64dbg's.

## Virtualized code: the method of VMHunt

`vm.*` finds and simplifies **virtualized code** in a recorded trace, with the method of [VMHunt](https://github.com/s3team/VMHunt) (Xu et al., *VMHunt: A Verifiable Approach to Partially-Virtualized Binary Code Simplification*, CCS 2018), reimplemented here on Fathom's traces (VMHunt's own code, for Linux traces of Pin, has no license):

```
StartTraceRecording "C:\traces\vm.trace64"     // record the protected function (Trace menu, or these commands)
TraceIntoConditional cip == ret, .1000000
StopTraceRecording
vm.detect "C:\traces\vm.trace64"               // the virtualized snippets of the trace
vm.simplify 0                                   // what snippet 0 computes, without the virtual machine
```

- **Detection**: a virtual machine enters by saving the whole context — a burst of pushes of nearly every general register (12 of the 16 of x64, 7 of the 8 of x86; a function prologue saves at most the non-volatile ones) or `pushad` — and leaves by restoring it with a burst of pops. In between, a **dispatcher** (an indirect `jmp`, or a `ret` used as one) runs again and again to the **handlers**. `vm.detect` lists each snippet: thread, instructions, entry and exit, dispatcher address, dispatches and handlers. A context save without a dispatcher is not counted.
- **Simplification**: `vm.simplify n` replays the snippet in Triton with the general registers at its entry as symbolic inputs (`rax_in`, `rbx_in`...), and prints each register at its exit as an expression of them, simplified by Z3 and, when Triton's synthesis finds one, as the simpler equivalent. The session stays open: `sym.slice`, `sym.expr`, `sym.solve` work on the result.

The test program [`tests/vm_target.c`](tests/vm_target.c) is a small virtual machine in the style of the protectors (context save, bytecode, handler table, context restore) that computes `eax = (eax ^ 0x5A) + ebx * 3`. In its trace `vm.detect` finds one snippet (5 dispatches to 5 handlers), and `vm.simplify 0` prints the expression Z3 leaves after the virtual machine, a tangle of bit extractions, and Triton's synthesis gives back `((eax_in ^ 0x5a) + ((ebx_in * 0x3) & 0xffffffff)) & 0xffffffff`.

| Command | Effect |
|---|---|
| `vm.detect [file]` | the virtualized snippets of a trace (default: the trace of the `sym.trace` session); `$result` = their number |
| `vm.list` | the snippets found |
| `vm.simplify n` | snippet `n` replayed with symbolic registers at its entry: the registers at its exit, simplified and synthesized; `$result` = the registers computed from the inputs |

## How it works

- **Debuggee source**: the registers come from the debugger once, at `sym.start`; a memory page is read from the process the first time Triton needs it (then Triton keeps its own copy, with what the emulated code wrote). Triton stops at a **system call** (`syscall`, `int`, `sysenter`: Triton does not run the kernel), at an instruction it does not support, at unreadable memory, at `end` or `count`. For a whole program with its system calls, threads and windows, the emulator (`emu.*`) is the tool; the Triton plugin analyzes what a piece of code computes.
- **Trace source**: before each instruction of the thread, Triton's registers and the memory the instruction reads are set to the recorded values when they differ (another thread, an API call skipped while tracing, or the kernel changed them). If such a value was symbolic, it becomes concrete there (`sym.info` counts them): the input no longer flows through code that is not in the trace. The trace decides where the thread goes next.
- **Symbolic execution** only where symbolic data flows (Triton's `ONLY_ON_SYMBOLIZED`): the rest runs concretely, and only the branches that depend on the input are path constraints. Memory addresses are concrete (a symbolic pointer is concretized), the usual choice of dynamic symbolic execution.
- **Solver**: Z3 4.16 (`libz3.dll` next to the plugin), 60 s per query.
- One thread at a time: the debugger's current thread, or the trace's thread.

## Building

`triton\build.ps1` (Visual Studio 2022, CMake 3.20+, Ninja) builds `triton\build-x64` and `triton\build-x32`: Triton from [`third_party/Triton`](third_party/Triton/FATHOM.md) (static), Capstone 5.0.7 from its sources (static, x86 only) and the official Z3 4.16.0 build (`libz3.dll`), both downloaded on the first configure with pinned SHA-256 hashes. Point `-Sdk` at the `pluginsdk` folder of a Fathom / x64dbg build.

```
powershell -File triton\build.ps1 -Sdk C:\path\to\pluginsdk
```

The plugin goes into `x64\plugins` / `x32\plugins` with its `libz3.dll`, which it loads from its own folder (a missing `libz3.dll` leaves the plugin without commands, with a message in the log).

## Tests

[`tests/test_triton.py`](tests/test_triton.py) runs [`tests/target.c`](tests/target.c) in `headless.exe`, x64 and x32 (20 assertions, and 3 more for the virtual machine of [`tests/vm_target.c`](tests/vm_target.c) on x32): the serial of a branchless check solved, applied and accepted by the debuggee natively; the first branch of a per-character check flipped, the next run one comparison further; `(x ^ y) + 2 * (x & y)` synthesized to `x + y`; taint; a recorded trace of the check replayed and its serial solved; `vm.detect` on that trace (no virtual machine); on x32, the virtual machine detected and its computation recovered by `vm.simplify`.

```
py triton\tests\test_triton.py --bin bin\x64 --build triton\build-x64
py triton\tests\test_triton.py --bin bin\x32 --build triton\build-x32
```

## Licenses

Triton: Apache-2.0, Copyright Jonathan Salwan and Quarkslab ([`third_party/Triton/LICENSE.txt`](third_party/Triton/LICENSE.txt)), vendored unmodified. Z3: MIT, Microsoft. Capstone: BSD-3-Clause, Nguyen Anh Quynh. The plugin's own code: GPL-3.0-or-later, like Fathom.
