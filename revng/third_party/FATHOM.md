# rev.ng in Fathom

`revng/`: [rev.ng](https://github.com/revng/revng) (branch `develop`, taken on 2026-11-26), without `tests/` (not built
on Windows) and its CI files. Its files are MIT, rev.ng as a whole GPL-2.0. The changes, all for Windows (MSYS2
CLANG64), are in [`../patches/revng.patch`](../patches/revng.patch) too; they leave the Linux build as it was.

## Build (CMake)

- `share/revng/cmake/Common.cmake`: on Windows every library is an OBJECT library; `revng_win32_link_all()` links all
  of them, statically with LLVM, MLIR and clang, into the Python module and each program, in dependency order
  reversed (MinGW runs static constructors in the reverse order of the link: a library is initialized before its
  users, as shared libraries are); an analyses library declared with a type is handled.
- `CMakeLists.txt`: x86 / x86-64 only (the libtcg built), `revng-opt.exe` instead of `revng opt` with plugins, the
  clang of LLVM 16 for the early-linked module (with MinGW's headers), no `support.c` (runtime of recompiled Linux
  programs), no docs / tests / TypeScript client, the component hash when configuring, LLVM's definitions split,
  nanobind from rev.ng's fork (`REVNG_NANOBIND_DIR`), the Python packages in the prefix's `lib/pythonX.Y`, no
  `-Werror`, no exported CMake targets.
- `share/revng/cmake/TupleTreeGenerator.cmake`: the generators run with `Python3_EXECUTABLE`, their output split in
  CMake (no `tr`, `\r\n` handled), no shell quotes around type names (cmd runs custom commands).
- `python/CMakeLists.txt`: `_pipebox.pyd` holds all of rev.ng; no wheels (the generated Python files they pulled are
  built with `ALL`, except `mixins.py` and `pipeline_description.py`, of the `revng.project` Python API, which run
  rev.ng's command line or `yq`). `lib/Yield/CMakeLists.txt`: the X86
  disassembler only. `tools/win32-opt/` (new): `revng-opt`, LLVM 16's `opt` (its sources) with rev.ng linked in.
  `lib/Support/Win32LLVMDump.cpp` (new): `llvm::Value::dump()`, absent from LLVM built without assertions.

## Platform

- `lib/Support/PathList.cpp`: the root found from the module that contains rev.ng (`GetModuleFileNameW`), no
  `dl_iterate_phdr`. `lib/Lift/LibTcg.cpp`: `libtcg-<arch>.dll`.
- `lib/Support/InitRevng.cpp`, `include/revng/Support/InitRevng.h`: `llvm::InitLLVM` replaces the arguments with the
  process's command line on Windows; the Python module's own arguments are kept.
- `include/revng/Support/TemporaryFile.h`: the descriptor of a temporary file closed (it leaked), and a read-only
  file made writable before it is deleted (Windows deletes neither an open nor a read-only file).
- `lib/Support/Configuration.cpp`: `glob` with LLVM's `GlobPattern`. `lib/Support/Progress.cpp`: the working set of
  the process instead of `/proc`. `lib/Support/OnQuit.cpp`, `lib/PipeboxCommon/Python/_pipebox.cpp`,
  `python/revng/internal/pipebox.py`: the signals Windows has. `include/revng/ADT/LazySmallBitVector.h`: the
  compiler's `ffs` builtins. `include/revng/Support/Tar.h`, `lib/Support/Tar.cpp`: libarchive's `la_ssize_t`
  (`long` is 32-bit on Windows).
- `include/revng/TupleTree/Visits.h`: the tuple indices of a path read as `size_t`, as they are pushed (`unsigned
  long` is 32-bit on Windows). `lib/HelperArgumentsAnalysis/FixHelpers.cpp`: `std::max<int64_t>`.
- `lib/Model/Register.cpp`: the offsets of the vector registers in QEMU's CPU structure, 0x30 bytes further on
  Windows (checked by libtcg's static assertions).
- `lib/Model/Importer/Binary/PECOFFLDDTree.cpp` (code built only on Windows): `PATH` split on `;`, and
  `llvm::sys::path::append`.
- `lib/Lift/ExternalJumpsHandler.cpp`: its `Log` local (one binary: no duplicate symbol).
- `share/revng/early-linked.c`: a `setjmp` function declared (MinGW's is a macro).
- `python/...`: `uvloop` and `fcntl` optional (`hypercorn.py`, `daemon_manager.py`), the database migrations found
  on `sys.path` without the package's metadata (`db_migrator.py`), `os.pathsep` (`scripts/revng`).

## libc++ and LLVM

- `include/revng/ADT/TraitfulAny.h`: the macros of libc++ of LLVM 16's time defined when a newer libc++ lacks them.
  `include/revng/Support/ExplicitSpecializations.h` / `.cpp`: the explicit instantiation of a libc++ internal only
  with that libc++. Missing includes (`Concepts.h`, `RecursiveCoroutine-coroutine.h`, `Statistics.h`,
  `DynamicHierarchy.h`).
- `lib/DataLayoutAnalysis/Backend/DLAUpdateModelTypes.cpp`: a local `getNameOrAsOperand` (LLVM's exists only with
  assertions).

## The helpers built for Windows

QEMU's helpers compiled for Windows follow the Windows x64 ABI and MinGW's headers:

- `lib/HelperArgumentsAnalysis/FixHelpers.cpp`: loads and stores of vectors (128-bit values) or `x86_fp80` made
  integer ones of the same size; `memmove` decomposed as `memcpy`.
- `lib/HelperArgumentsAnalysis/CPULoopExitPass.cpp`: zero returned for floating point and vector types.
  `CPUStateUsage.h`: the size of such accesses. `Function.h`: metadata operands (`llvm.experimental.noalias.scope.decl`)
  unknown values.
- `lib/Lift/VariableManager.cpp`: `memset` with another byte or of a non-integer variable, a constant pointer.
