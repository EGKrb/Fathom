# Ghidra in Fathom

Parts of [Ghidra](https://github.com/NationalSecurityAgency/ghidra) (branch `master`, taken on 2026-11-25), used
by the Decompiler plugin of Fathom (`decompiler/`). Apache-2.0: [`LICENSE`](LICENSE), [`NOTICE`](NOTICE).

| Here | In Ghidra |
|---|---|
| `decompile/cpp/` | `Ghidra/Features/Decompiler/src/decompile/cpp/` (the decompiler and sleigh, C++) |
| `decompile/zlib/` | `Ghidra/Features/Decompiler/src/decompile/zlib/` |
| `x86/languages/` | `Ghidra/Processors/x86/data/languages/` (the x86 sleigh specifications) |

Everything else of Ghidra (the Java application, the other processors, the unit tests of the decompiler) is
left out.

**One change**, in `decompile/cpp/slghscan.l` and the scanner generated from it, `slghscan.cc`: `scan_number`
parses with `std::stoull` instead of `std::stoul`. With MSVC, `unsigned long` is 32 bits, so the sleigh compiler
rejected every 64-bit constant of the x86 specifications ("Parsed integer is too big"); Ghidra compiles its
specifications with its Java sleigh compiler on Windows and does not meet it.

To update: replace these folders with those of a newer Ghidra, keep the change above, and run
`decompiler/tests/test_decompiler.py` on x64 and x32. Do not reformat these files.
