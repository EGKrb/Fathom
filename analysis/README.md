# angr and miasm on the debuggee

The `angr.*` and `miasm.*` commands of ExtraCmds run [angr](https://github.com/angr/angr) and [miasm](https://github.com/cea-sec/miasm) on the debuggee **as it is now**: the command saves the process to a minidump (x64dbg's `minidump` command, in `%TEMP%\Fathom\analysis`, deleted afterwards) and the scripts of this folder load it, so unpacked and patched code is analyzed, and so is the state of the current thread.

## angr: path exploration

```
angr.symbolic rcx, 8                          // the 8 bytes rcx points to are the input (angr.symbolic alone: none)
angr.explore mymodule.check + 0x2A, mymodule.check + 0x30   // reach the first address, never the others
angr.apply                                    // writes the input found into the debuggee
```

`angr.explore find[, avoid ...]` loads the dump in angr (cle's minidump backend), starts from the registers of the debugger's current thread, and explores the paths until one reaches `find` without going through `avoid`; the input is solved and written to the log (hexadecimal and text), `$result` = 1 if found. Unlike the Triton plugin, which follows one path and flips its branches, angr explores **all the paths** at once. 5 minutes at most by default.

## miasm: IR and symbolic execution

```
miasm.ir mymodule.decrypt          // the function lifted to miasm's intermediate representation
miasm.symb mymodule.mba            // what it computes: final registers and memory, simplified
miasm.symb 0x140001000, 0x140001080
```

`miasm.symb` executes the function symbolically from its entry (to `end`, or until its return or a branch that depends on its inputs) and prints each register and memory location it changed as an expression of the initial values, simplified by miasm. For `(x ^ y) + 2 * (x & y)`: `RAX = {(RCX[0:32] & RDX[0:32]) * 0x2 + (RCX[0:32] ^ RDX[0:32]) 0 32, 0x0 32 64}`.

## Setup

`analysis.setup` installs the packages (`pip install --user -r analysis\requirements.txt`, without `--user` in a virtual environment): angr (a few hundred MB), and `future` and `pyparsing` for miasm. miasm itself is in [`miasm/`](miasm) (the branch `master` of [cea-sec/miasm](https://github.com/cea-sec/miasm), taken on 2026-11-25, unmodified, without its tests and examples: more recent than the release on PyPI). The minidump reader is the emulator's (`emulator\minidump`). Python 3.10 or later on the `PATH`.

The scripts also work alone: `python fathom_angr.py explore app.dmp --thread 1234 --find 0x140001032 --symbolic 0x14fd30:8`, `python fathom_miasm.py symb app.dmp 0x140001000`.

## Test

`extracmds/tests/test_tools.py` (headless, x64 and x32): `miasm.ir` and `miasm.symb` on the target of the Triton plugin, and angr solving the serial of its `check_branches` from the debuggee's state; the debuggee then accepts it natively.

## Licenses

miasm: GPL-2.0-or-later ([`LICENSE.miasm`](LICENSE.miasm)). angr: BSD-2-Clause, installed with pip (not redistributed). The scripts: GPL-3.0-or-later, like Fathom.
