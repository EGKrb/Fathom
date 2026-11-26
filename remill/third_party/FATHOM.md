# remill in Fathom

`remill/`: [remill](https://github.com/lifting-bits/remill) (branch `master`, taken on 2026-11-26), Apache-2.0,
without its CI files, `flake.nix`, `flake.lock` and `Dockerfile`.

Changes, for Windows:

- `lib/BC/Util.cpp`: `DefaultSemanticsSearchPaths()` first looks in `semantics\` next to the executable
  (`GetModuleFileNameA` on Windows, `/proc/self/exe` elsewhere), before the build and install paths compiled
  in, so that a copy of `remill-lift` works wherever it is installed.
- `dependencies/llvm.cmake`: `LLVM_ENABLE_PROJECTS` is `clang` (remill compiles its semantics with clang; lld
  and clang-tools-extra are not used), and zlib / zstd are disabled (no compression in remill, no DLL to ship).

The build itself (`../build.ps1`) passes the Windows options: clang-cl of the superbuild's LLVM for remill,
`-DREMILL_FETCH_SLEIGH=OFF` (the sleigh of the superbuild), `-D_ALLOW_COMPILER_AND_STL_VERSION_MISMATCH`.
