# Triton in Fathom

This folder is a copy of [Triton](https://github.com/JonathanSalwan/Triton) (branch `master`, build number 1599
in `.build_number`, version 1.0), taken on 2026-11-25, used by the Triton plugin of Fathom (`triton/`).

**Unmodified.** Only the files needed to build the library are kept:

- `CMakeLists.txt`, `CMakeModules/`, `.build_number`, `LICENSE.txt` (Apache-2.0), `README.md`;
- `src/CMakeLists.txt` and `src/libtriton/` (the library, its Python bindings included, not built here);
- `doc/` without its figures (`doc/CMakeLists.txt`, included by the top-level CMake file, defines the
  documentation targets).

Left out: `src/examples/` (77 MB of samples and binaries), `src/testers/`, `src/scripts/`, `src/stubs/`,
`src/misc/`, `publications/`, `doc/figures/`, the CI files, `Dockerfile`, `setup.py`, `vcpkg*`.

`triton/CMakeLists.txt` builds it as a static library with Z3 and Capstone (no Boost: Triton's own
wide-integer headers, no LLVM, no Bitwuzla, no Python bindings).

To update: replace the files above with those of a newer Triton, keeping this list, and run
`triton/tests/test_triton.py` on x64 and x32. Do not reformat these files (Fathom's `format.bat` would
format every C/C++ file of the repository): they stay identical to Triton's.
