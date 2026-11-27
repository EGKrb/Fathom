# remill: lifting the debuggee's code to LLVM IR

Also as a repository of its own, with its releases: [fathom-remill](https://github.com/EGKrb/fathom-remill).

[remill](https://github.com/lifting-bits/remill) (Trail of Bits) translates machine code into LLVM bitcode: each instruction becomes calls to its semantics, written in C++ and compiled to LLVM IR, over a `State` structure that holds the registers. Fathom builds it for Windows (remill targets Linux and macOS) and the `remill.lift` command of ExtraCmds lifts a function of the debuggee:

```
remill.lift                          // the function at the selection (else cip): x64dbg's analysis gives its end
remill.lift mymodule.decrypt, 0x80   // 0x80 bytes from there
```

The code is read from the process memory (unpacked, decrypted or patched code as it is now), lifted by `remill-lift-17` with the semantics of the debuggee's architecture (`amd64_avx` in x64dbg, `x86_avx` in x32dbg), and the LLVM IR is written to the log and to `%TEMP%\Fathom\remill\<address>.ll`, ready for LLVM's tools (`opt -O2`, a symbolic executor on LLVM IR, KLEE...). `$result` = its number of lines.

## Building

`remill\build.ps1` (Visual Studio 2022, CMake, Ninja, git, Python) does what remill's own instructions do, for Windows:

1. its dependency superbuild (`third_party/remill/dependencies`): LLVM 17.0.6 with clang, XED, glog, gflags, googletest and sleigh, compiled with MSVC into `C:\Users\<you>\remill-deps` (a short path: LLVM's object paths exceed 260 characters otherwise). Several hours the first time;
2. remill itself with the clang-cl of that LLVM (remill uses `__int128` and GCC attributes that MSVC lacks), against the sleigh of the superbuild (`-DREMILL_FETCH_SLEIGH=OFF`), with `_ALLOW_COMPILER_AND_STL_VERSION_MISMATCH` (Visual Studio's C++ library asks for clang 19);
3. `remill\dist`: `remill-lift-17.exe` and the x86 / x64 semantics next to it (`semantics\*.bc`). LLVM is built without zlib and zstd, so the executable needs only Windows and the Visual C++ runtime.

## Changes to remill

See [`third_party/FATHOM.md`](third_party/FATHOM.md): the semantics are also looked for next to the executable (`semantics\`), so that remill works wherever it is installed; LLVM is built with clang only (not lld and clang-tools-extra, which remill does not use), without zlib and zstd.

## Test

`extracmds/tests/test_tools.py` (headless, x64 and x32): `remill.lift` on a function of the test program, read from memory; the IR is checked.

## Licenses

remill: Apache-2.0 ([`third_party/remill/LICENSE`](third_party/remill/LICENSE)). LLVM: Apache-2.0 with LLVM exceptions; XED: Apache-2.0; glog, gflags: BSD-3-Clause; sleigh / Ghidra: Apache-2.0.
