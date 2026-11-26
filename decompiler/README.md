# Decompiler plugin: Ghidra's decompiler in the debugger

The **Decompiler** plugin (`Decompiler.dp64` / `Decompiler.dp32`) runs [Ghidra](https://github.com/NationalSecurityAgency/ghidra)'s decompiler — its native C++ engine, the one Ghidra itself drives — on the code of the debuggee, from the debugger:

```
decompile                                  // the function at the selection in the disassembly (else cip)
decompile mymodule.check, "C:\out\check.c" // and the C written to a file
decompile.raw mymodule.check               // Ghidra's p-code listing of the function
```

```c
uint4 greet(xunknown8 param_1)
{
  uint4 uVar1;
  uint4 uVar2;
  xunknown1 axStack_48 [64];

  wsprintfA(axStack_48,0x7ff7317a2280,param_1);  /* "hello %s" */
  OutputDebugStringA(axStack_48);
  uVar1 = lstrlen(axStack_48);
  uVar2 = GetCurrentProcessId();
  return uVar2 ^ uVar1;
}
```

- **The code comes from the process memory**, not from the file on disk: unpacked, decrypted or patched code decompiles as it is now, and so does code that exists only in memory (shellcode, JIT, a manually mapped module).
- **Read-only memory is constant** (Ghidra's "readonly" option): calls through the import table become calls to their target, jump tables resolve.
- **Names**: functions are named after x64dbg's labels and symbols (exports, PDB, your labels); string literals and labelled addresses get a comment after the line (`/* "hello %s" */`).
- **The function** around an address comes from x64dbg's analysis, else (x64) from the exception directory of the module, else the address itself is taken as the entry.
- `$result` = the number of lines. x64dbg (x86-64, Visual Studio calling convention) and x32dbg (x86) alike.

## How it is built

`decompiler\build.ps1` (Visual Studio 2022, CMake, Ninja; nothing is downloaded) compiles from [`third_party/ghidra`](third_party/ghidra/FATHOM.md):

- the decompiler library (the `CORE`, `DECCORE` and `SLEIGH` groups of Ghidra's Makefile, plus `sleigh_arch`, `inject_sleigh`, `libdecomp`, and Ghidra's zlib), linked into the plugin;
- Ghidra's sleigh compiler, run at build time on the x86 specifications to produce `x86.sla` and `x86-64.sla`, which go with the `.ldefs`, `.pspec` and `.cspec` files into `decompiler\` next to the plugin.

The plugin implements a Ghidra `Architecture` whose `LoadImage` reads the debuggee's memory (and lists its read-only pages) and whose global scope names functions from x64dbg's labels as the decompiler meets them.

## Test

[`tests/test_decompiler.py`](tests/test_decompiler.py) (headless, x64 and x32): a loop with its constants, a `switch` returning string literals, and Windows API calls through the import table, decompiled from the running [`tests/target.c`](tests/target.c).

## Licenses

Ghidra: Apache-2.0 ([`third_party/ghidra/LICENSE`](third_party/ghidra/LICENSE), [`NOTICE`](third_party/ghidra/NOTICE)); its zlib: zlib license. The plugin's own code: GPL-3.0-or-later, like Fathom.
