# NoVmp and VTIL in Fathom

- `NoVmp/`: [NoVmp](https://github.com/can1357/NoVmp) (branch `master`, taken on 2026-11-25), GPL-3.0, without its
  `assets/` and its git submodules (VTIL-Core and linux-pe come from below).
- `VTIL-Core/`: [VTIL-Core](https://github.com/vtil-project/VTIL-Core) (branch `master`, taken on 2026-11-25),
  BSD-3-Clause, without `Sample Routines/` (one of them is in `../tests/`).
- linux-pe: fetched by `../CMakeLists.txt` at the commit NoVmp's submodule pins (`30aae563`, no branch holds it
  any more: GitHub's archive of it, SHA-256 pinned).

NoVmp was written against an older VTIL (the commit `25861ca` of its submodule). **Changes** to NoVmp for the
VTIL of today, where a routine owns its blocks (`std::unique_ptr`) and `routine::clone()` returns a
`std::unique_ptr`:

- `NoVmp/demo_compiler.hpp`: `block.get() == rtn->entry_point`, `explored_blocks[...].get()` (twice),
  `backup = rtn->clone()`;
- `NoVmp/vmprotect/vtil_lifter.cpp`: `explored_blocks[ entry_vip ].get()`.

VTIL is unchanged.
