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


**Changes for x86** (Fathom's port: `NoVmp32.exe`, NoVmp built as a 32-bit program, where VTIL's native
architecture is x86):

- `NoVmp/vmprotect/native.hpp` (new): what depends on the native architecture (register size, native registers,
  `pushfd`/`pushfq`, PE32/PE32+ headers, TEB segment, self-references of the image, relocated loads, base of the
  virtual instruction pointer, which `-vipbase` sets);
- `NoVmp/vmprotect/architecture.cpp`, `subroutines.cpp`, `vtil_lifter.cpp`, `il2vtil.cpp`, `image_desc.hpp`,
  `deobfuscator.hpp`, `vm_state.hpp`: the 8-byte sizes, `rsp`/`rax`/... and `gs:` replaced by those of
  `native.hpp`; the context saved at the VM entry has 8 + 2 slots on x86;
- `NoVmp/emulator/emulator.cpp`: the native emulator in x86 assembly for the 32-bit build (the x64 shellcode is
  unchanged); `NoVmp/emulator/rwx_allocator.cpp`: `_WIN32` instead of `_WIN64`;
- `NoVmp/main.cpp`: the `-vipbase` option; the demo compiler (x64 code generation) is left out of the x86 build.

VTIL, for x86: `VTIL-Architecture/routine/basic_block.cpp` assembles `vemit` with the x86 assembler when the
routine's architecture is x86 (it only handled amd64).

**VMProtect version handling** (`NoVmp/vmprotect/subroutines.cpp`, `parse_vmenter`):

- on x86 the relocation load is `mov r32, imm32`, which also matches a junk `mov edi, 0`; it now requires the
  immediate to be an address inside the image (its real or preferred base). x64's `movabs` is unchanged;
- VMProtect 3.6 – 3.11 save the context at the VM entry by a mix of `push r` and `mov [rsp + off], r` and have no
  such reloc load in the push block: that is detected and reported (`vtil::logger::error`, caught per VM) instead
  of asserting on a wrong context. `NOVMP_DEBUG_VMENTER=1` dumps the deobfuscated VM-entry stream.
