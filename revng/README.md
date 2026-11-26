# rev.ng: decompiling the debuggee's functions

[rev.ng](https://rev.ng) (rev.ng Labs) is a binary analysis framework and decompiler built on QEMU and LLVM: QEMU's translators (TCG) lift the machine code, LLVM analyses recover the functions, their prototypes and their types, and its decompiler emits C. rev.ng runs on Linux only; Fathom ports it to Windows, natively, and the `revng.decompile` command of ExtraCmds decompiles a function of the debuggee with it:

```
revng.decompile                      // the function at the selection (else cip)
revng.decompile mymodule.decrypt     // the function that contains this address
```

- The module is read **from memory** (as for `novmp` and `binja`: unpacked, decrypted, at its load address) and written as a PE file in `%TEMP%\Fathom\revng\<module>_<base>\`.
- rev.ng's project of that image is created once (`revng project init`: the initial auto-analysis of the whole module, a few seconds for a small one, longer for a large one), then the `emit-c` artifact of the function (by its entry, `0x...:Code_x86_64` or `Code_x86`) is produced, without its markup (`revng ptml -p`). The C is in the log and in `<address>.c` next to the project; `$result` = its lines.
- x64 modules (Microsoft x64 ABI) and x86 ones (cdecl, stdcall...): QEMU's x86-64 and i386 translators are built.

rev.ng is in `revng\` of the release (`revng\dist` in the repository), self-contained: its Python and the packages it uses, rev.ng's Python package with its native module, QEMU's translators (libtcg) and the DLLs they use. `revng\revng.cmd` is rev.ng's command line (`revng.cmd project init <file>`, `revng.cmd project artifact emit-c <function>`, `revng.cmd --help`).

## The Windows port

rev.ng is built with LLVM 16 (rev.ng's fork, with clang and MLIR) and libtcg (QEMU 8.2, rev.ng's fork, built as a library), and loads its analyses as shared libraries. What the port does:

- **libtcg** ([`patches/qemu.patch`](patches/qemu.patch)): QEMU's user-mode code without linux-user (which libtcg only used for the initial state of the CPU): the x86 CPU is set up directly, and what linux-user provided to the rest is in `libtcg/win32-user.c`. Built with MSYS2 UCRT64 (gcc). QEMU's CPU structure is 0x30 bytes larger on Windows: the offsets rev.ng relies on are checked and shifted accordingly on both sides.
- **The helpers' bitcode** (the C functions translated code calls, in LLVM IR, which rev.ng inlines and analyzes): built on Linux from an emulator binary; here `libtcg/win32/gen-helpers.py` compiles them with LLVM 16's clang and the options of the Windows libtcg (so the structures match), with `libtcg/win32/helpers-win32.c` for what linux-user gave them.
- **rev.ng** ([`third_party/FATHOM.md`](third_party/FATHOM.md)): built with MSYS2 CLANG64 (clang and libc++, the C++ library rev.ng is written for). Its libraries are linked statically into its Python module and each of its programs, instead of shared libraries loaded as plugins (each would carry its own copy of LLVM's and MLIR's registries on Windows); what assumed Linux (paths, signals, `/proc`, `glob`, file deletion, the 64-bit `long`, the System V ABI of the helpers) is adapted.

## Building

`revng\build.ps1` (MSYS2, from scoop or `C:\msys64`; git) runs [`build.sh`](build.sh) in MSYS2: the packages (pacman), rev.ng's forks of QEMU, LLVM and nanobind at pinned commits (`patches/qemu.patch` applied), libtcg, LLVM 16, the helpers, a Python environment, rev.ng ([`third_party/revng`](third_party/revng)), then `revng\dist` ([`stage.sh`](stage.sh)). Its work folder is `%USERPROFILE%\revng-build` (`-Work`); `-Steps libtcg,revng,stage` runs some of the steps. The first build takes hours (LLVM with clang and MLIR), about 10 GB.

## Test

`extracmds/tests/test_tools.py` (headless, x64 and x32): `revng.decompile` on a function of the test program read from memory (`mba`, which computes `(x & y) * 2 + (x ^ y)`): its C is checked.

## Licenses

rev.ng: its files are MIT, rev.ng as a whole is GPL-2.0 (QEMU) ([`third_party/revng/LICENSE`](third_party/revng/LICENSE), [`LICENSE.mit`](third_party/revng/LICENSE.mit)). QEMU / libtcg: GPL-2.0 (rev.ng's fork, changed: `patches/qemu.patch`); LLVM: Apache-2.0 with LLVM exceptions; nanobind (rev.ng's fork): BSD-3-Clause. In `revng\dist`: Python (PSF License) and the packages rev.ng uses (each with its license, in `lib\python3.14\site-packages`), glib (LGPL-2.1+), Capstone (BSD-3-Clause), and the MinGW runtime DLLs of MSYS2.
