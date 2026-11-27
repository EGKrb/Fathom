# NoVmp: devirtualizing VMProtect from the debugger

Also as a repository of its own, with its releases: [fathom-novmp](https://github.com/EGKrb/fathom-novmp).

[NoVmp](https://github.com/can1357/NoVmp) by Can Bölük devirtualizes the virtual machines of **VMProtect 3.0 – 3.5** into optimized [VTIL](https://github.com/vtil-project/VTIL-Core) (the Virtual-machine Translation Intermediate Language). NoVmp itself only handles x64 programs; Fathom ports it to x86 as well and builds it twice, `NoVmp.exe` (x64) and `NoVmp32.exe` (x86), each with its `vtildump` (`vtildump.exe`, `vtildump32.exe`), which prints VTIL routines as text. The `novmp` command of ExtraCmds drives it on the debuggee:

```
novmp                                 // the module of the selection (else cip)
novmp protected.exe                   // a module by name or address
novmp protected.exe, 0x729B81         // only these VM entries (RVAs of the VMEnter)
novmp protected.exe, vipbase:0        // x86: the base the virtual instruction pointer is relative to
```

- NoVmp needs an **unpacked** image: the command reads the module **from memory** (as it is now, unpacked by the program itself), turns it into a PE file whose sections are at their virtual addresses, with the image base where the module is loaded, and gives it to NoVmp (`%TEMP%\Fathom\novmp\<module>_<base>\`).
- NoVmp finds the VM entries (`jmp`/`call` to a `push imm; call` in another executable section), lifts each virtual machine to VTIL and optimizes it; its output is in the log. The routines are saved in `vms\<rva>.premature.vtil` and `vms\<rva>.optimized.vtil`; with up to four of them, `vtildump` prints the optimized ones in the log. `$result` = the number of routines lifted.
- x64 modules (PE32+, in x64dbg) go to `NoVmp.exe`, x86 modules (PE32, in x32dbg) to `NoVmp32.exe`.

## The x86 port

VTIL chooses its native architecture when it is compiled (`_M_IX86`): built as a 32-bit program, its native registers, stack pointer, pointer size and assembler are those of x86. NoVmp's own code assumed x64 throughout; [`vmprotect/native.hpp`](third_party/NoVmp/NoVmp/vmprotect/native.hpp) gathers what depends on the architecture, and the lifter uses it everywhere:

- the size of a native register (4 or 8 bytes) in the virtual stack: pushes and pops of the VM, the operand sizes of the handlers, `pushfd` / `pushfq`, the context saved at the VM entry (8 + 2 slots on x86 instead of 16 + 2);
- the native registers (`esp` / `rsp`, `eax` / `rax`...), the TEB (`fs:` / `gs:`), the PE headers (PE32 / PE32+);
- the references VMProtect makes to its own image: `mov r32, imm32` with the relocated address on x86 (x64 uses `lea` relative to `rip`), the `mov` of a relocated constant instead of `movabs`;
- the base of the virtual instruction pointer: VMProtect x64 adds 0x100000000 to it; for x86 the default is 0, and the `vipbase:` argument of the command (NoVmp's `-vipbase` option) sets another value;
- the emulator, which runs the decryption blocks of the VM natively to compute the next instruction pointer: x86 assembly in the 32-bit build (`emulator/emulator.cpp`), checked by [`tests/emulator_test.cpp`](tests/emulator_test.cpp) on random blocks (registers, 8 / 16-bit parts, flags);
- VTIL's x86 code generation in `basic_block` (`vemit` assembled with the x86 assembler).

The x86 port is built, its emulator tested, and it was run against a program protected with the official **VMProtect 3.11 demo** (its own test program, [`tests/vmp_target.c`](tests/vmp_target.c), unpacked first): NoVmp32 finds the VM entries, so the x86 port's detection, image handling and VM-entry discovery work. The x64 lifter behaves as before.

## VMProtect versions

NoVmp devirtualizes **VMProtect 3.0 – 3.5**. The VMs of **3.6 – 3.11** are different: the context is saved at the VM entry by a mix of `push r` and `mov [rsp + off], r` (not every register is pushed), the relocation delta is no longer loaded in the push block, and the self-reference is a `lea` of an absolute address rather than a `mov r, imm`. Fathom's NoVmp **detects a 3.6 – 3.11 VM entry and reports it clearly** (instead of asserting deep in the lifter on a wrong context), and the `novmp` command surfaces that message. Lifting a 3.6 – 3.11 VM would require reworking the VM-entry parser, the context model and the handler analysis — a separate, large effort. Set `NOVMP_DEBUG_VMENTER=1` to dump a VM entry's deobfuscated stream (useful for that work). A **genuine x86 fix** came out of this: on x86 the relocation load is a bare `mov r32, imm32`, so NoVmp now requires its immediate to be an address inside the image, where before a junk `mov edi, 0` could be mistaken for it.

## Building

`novmp\build.ps1` (Visual Studio 2022, CMake, Ninja, git) builds `novmp\build\NoVmp.exe` and `vtildump.exe` (64-bit), then `novmp\build32\NoVmp32.exe` and `vtildump32.exe` (32-bit; `-Arch x64` or `-Arch x86` builds only one), from [`third_party`](third_party/FATHOM.md). VTIL fetches Capstone and Keystone at the commits it pins, and linux-pe is fetched at NoVmp's commit (pinned SHA-256); the 32-bit build reuses the sources the 64-bit one fetched. `emulator_test.exe` is built with each.

## Test

`extracmds/tests/test_tools.py` runs `novmp` in x64dbg and in x32dbg on a module (read from memory, given to NoVmp: no virtual machine in an unprotected program) and `vtildump` on [`tests/vmprotect.vtil`](tests/vmprotect.vtil), a routine lifted from VMProtect (VTIL's sample, from [`tests/vmprotect.c`](tests/vmprotect.c)). `emulator_test` checks the native emulator of each build. No VMProtect-protected program is part of the tests: VMProtect is commercial, and a protected binary embeds its runtime. To test against VMProtect itself, [`tests/protect_vmp_target.ps1`](tests/protect_vmp_target.ps1) builds [`tests/vmp_target.c`](tests/vmp_target.c) (functions with known results, virtualized with the VMProtect SDK) and protects it with the VMProtect demo; NoVmp then runs on the unpacked image (NoVmp needs an unpacked module, as the `novmp` command reads it from memory).

## Licenses

NoVmp: GPL-3.0 ([`third_party/NoVmp/LICENSE`](third_party/NoVmp/LICENSE)). VTIL: BSD-3-Clause ([`third_party/VTIL-Core/LICENSE.md`](third_party/VTIL-Core/LICENSE.md)). Linked into the executables: Capstone (BSD-3-Clause), Keystone (GPL-2.0, or its commercial license), linux-pe (MIT).
