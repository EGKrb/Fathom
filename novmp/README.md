# NoVmp: devirtualizing VMProtect from the debugger

[NoVmp](https://github.com/can1357/NoVmp) by Can Bölük devirtualizes the virtual machines of **VMProtect x64 3.0 – 3.5** into optimized [VTIL](https://github.com/vtil-project/VTIL-Core) (the Virtual-machine Translation Intermediate Language). Fathom builds it (`NoVmp.exe`) with `vtildump.exe`, which prints VTIL routines as text, and the `novmp` command of ExtraCmds drives it on the debuggee:

```
novmp                                 // the module of the selection (else cip)
novmp protected.exe                   // a module by name or address
novmp protected.exe, 0x729B81         // only these VM entries (RVAs of the VMEnter)
```

- NoVmp needs an **unpacked** image: the command reads the module **from memory** (as it is now, unpacked by the program itself), turns it into a PE file whose sections are at their virtual addresses, with the image base where the module is loaded, and gives it to NoVmp (`%TEMP%\Fathom\novmp\<module>_<base>\`).
- NoVmp finds the VM entries (`jmp`/`call` to a `push imm; call` in another executable section), lifts each virtual machine to VTIL and optimizes it; its output is in the log. The routines are saved in `vms\<rva>.premature.vtil` and `vms\<rva>.optimized.vtil`; with up to four of them, `vtildump` prints the optimized ones in the log. `$result` = the number of routines lifted.
- x64 modules only (NoVmp lifts VMProtect's x64 virtual machine).

## Building

`novmp\build.ps1` (Visual Studio 2022, CMake, Ninja, git) builds `novmp\build\NoVmp.exe` and `vtildump.exe`, 64-bit, from [`third_party`](third_party/FATHOM.md); VTIL fetches Capstone and Keystone at the commits it pins, and linux-pe is fetched at NoVmp's commit (pinned SHA-256).

## Test

`extracmds/tests/test_tools.py` runs `novmp` on a module (read from memory, given to NoVmp: no virtual machine in an unprotected program) and `vtildump` on [`tests/vmprotect.vtil`](tests/vmprotect.vtil), a routine lifted from VMProtect (VTIL's sample, from [`tests/vmprotect.c`](tests/vmprotect.c)). No VMProtect-protected program is part of the tests: VMProtect is commercial.

## Licenses

NoVmp: GPL-3.0 ([`third_party/NoVmp/LICENSE`](third_party/NoVmp/LICENSE)). VTIL: BSD-3-Clause ([`third_party/VTIL-Core/LICENSE.md`](third_party/VTIL-Core/LICENSE.md)). Linked into the executables: Capstone (BSD-3-Clause), Keystone (GPL-2.0, or its commercial license), linux-pe (MIT).
