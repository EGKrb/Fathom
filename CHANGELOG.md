# Changelog

All notable changes to Fathom are documented here. Fathom is built on
[x64dbg](https://github.com/x64dbg/x64dbg) (GPL-3.0) and tracks its `development`
branch; this changelog covers Fathom's own additions and branding.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Changed
- **Faster recording**: tracing with trace recording is about 2.5 times faster
  (about 11,800 instead of 4,700 instructions per second with
  `TraceIntoConditional`, 19,000 instead of 8,900 without recording). TitanEngine
  caches the thread contexts while a debug event is handled (the threads of the
  debuggee are frozen until `ContinueDebugEvent`; every write invalidates the
  thread), uses its own thread handles instead of opening new ones and no longer
  suspends frozen threads to read a context. The recorder buffers its records
  (flushed every 250 ms, when the debugger pauses and when the recording stops).
  Setting `Engine` / `ContextCache` to 0 disables the cache.

### Added
- **Thread order of a trace**: the recorder samples every thread (CPU time and a
  hash of its user-mode registers, user block `0x82`) when the recording starts
  and stops, on each context switch and every 4096 instructions. `TraceExport`
  stores the intervals in the `.ftt` (chunk kind 4, ignored by older readers,
  same version) and in the JSON export (`cpu` member), and sets `$result1` /
  `$result2` to the sampled intervals and to those in which other threads
  executed code outside the trace. `tt.info`, `tt.seek` and the tooltips of the
  Time Travel tab tell whether the order of a context switch is exact or
  approximate, and which threads ran outside the trace.
- Tests: `src/tests/trace_threads` (a spinning worker makes the order
  approximate, a blocked one leaves it exact).

## [1.2.0] - 2026-11-04

### Added
- **Time Travel** — explore a trace backward and forward without leaving the
  debugger. New **`.ftt` format** (Fathom Time Travel, `docs/developers/ftt.md`):
  binary, LZ4-compressed and indexed, one timeline per thread synchronized by a
  global sequence number, explicit context-switch markers, register keyframes +
  strict deltas, and memory snapshots at regular intervals so the state at any
  instant is rebuilt on demand. **Time Travel tab** (ExtraCmds, Qt): per-thread
  timelines (click a lane to see that thread at that instant), instructions
  around the position with the context switches, registers with changes
  highlighted, memory rebuilt at that instant, previous / next execution of an
  address and write to memory, "show in CPU". **`tt.*` commands** for scripts and
  `headless.exe`: `tt.open`, `tt.info`, `tt.seek`, `tt.next` / `tt.prev`,
  `tt.tnext` / `tt.tprev`, `tt.thread`, `tt.regs`, `tt.mem`, `tt.find` /
  `tt.findback`, `tt.write` / `tt.writenext`, `tt.show`.
- **TraceExport**: `ftt` output format (`.ftt` extension) and `.ftt` input;
  extended registers at a chosen verbosity (`regs=gpr|fpu|sse|avx|all`) in JSON,
  CSV and `.ftt`; one thread's timeline (`thread=`); `snapshot=` interval; JSON
  `seq` and `switch` members, CSV `seq` column, `size` of memory accesses.
- Trace recording: a context-switch marker (user block `0x81`, timestamped) is
  written when the next instruction runs on another thread; existing readers
  skip it.
- Tests: `src/tests/trace_export` (`.ftt` read back by an independent parser),
  `extracmds/tests/test_time_travel.py` (the `tt.*` commands checked against a
  real trace, x64 and x86).
- **wineye 1.2.1** (`hvkdfusion/wineye.py`) — crash-dump debugger: `!analyze -v`,
  call stacks from kernel into user mode (`k`, `!thread`, `!process <pid> 7`),
  processors and contexts (`~`, `.thread`, `.cxr`, `.trap`, `.frame`),
  disassembly (`u`, `uf`, `ln`), source lines and locals (`dv`) with private
  PDBs (`--sympath`), user-mode minidumps (`.dump /u`), and a native engine
  that opens kernel `.dmp` files without MemProcFS. MCP tools `stack`,
  `analyze`, `disassemble`. Tests in `hvkdfusion/tests/test_debugger.py`.
- wineye: user-mode minidumps as targets; on kernel dumps without user pages,
  modules rebuilt from the VADs and the user-mode entry frame of each thread
  symbolized; `kb`, and `dv` without a private PDB (parameter home slots).
- wineye: the **32-bit code of WoW64 processes is unwound** (it was x64 only),
  with or without PDBs: FPO / FrameData programs of the PDB, otherwise the
  stack layout of the code itself (`/Oy` code included), bounded by the ebp
  chain; `k` marks the 64-bit / 32-bit transitions; `kb`, `dv`, `r`, `u`, `uf`
  and `!analyze` on x86 frames; x86 minidumps; 32-bit modules of WoW64
  processes (PEB32) and modules completed from the VADs.
- wineye: a processor without saved context (raw memory image, live target)
  gets the user-mode registers of its running thread from the trap frame at
  the top of its kernel stack.

### Changed
- HvKdFusion `setup` uses the components bundled in `release\hvkdfusion\`
  (`hvlib\`, `memprocfs\`, `ntoseye\`, `dokany\`): the LiveCloudKd source
  tree is no longer needed, MemProcFS is found next to the script, ntoseye
  runs through WSL, and Dokany is pointed to (never installed by Fathom).

### Fixed
- wineye: `vmm.dll` was never initialized (its argument parsing starts at
  index 0); kernel reads in a KVA-shadowed process context fall back to System.
- wineye: the rbx / rdi / rsi of a trap frame are no longer taken as known after
  an interrupt or an exception (the kernel saves them only on a system call).

### Security
- `src/cross/remote_server`: lief 0.17.2 (null pointer dereference in the ELF
  parser of 0.16.4).

## [1.1.0] - 2026-10-29

### Added
- **TraceExport** — two new output formats, the text traces of the IDA Pro
  trace explorers [Tenet](https://github.com/gaasedelen/tenet) (`tenet`, x86
  and x64) and [Frinet](https://github.com/synacktiv/frinet) (`frinet`, x64,
  chosen automatically for `.tenet` files, with an optional ASLR `slide`
  argument). pybridge's `export_trace()` accepts them, plus a `slide`.

## [1.0.0] - 2026-10-29

First release under the Fathom name.

### Added
- **HvKdFusion** — Hyper-V kernel debugging and memory forensics, driven from
  the command bar (`hvkd`, `hvkd.log`, `hvkd.check`, `wineye`) and from a
  dockable **HvKdFusion GUI tab** in the debugger (VM selector, component
  readiness, process explorer → modules / VADs / handles, Dump / Debug / Mount).
- **wineye** — a native-Windows, passive, WinDbg-style memory REPL on
  MemProcFS's `vmm.dll` (no WSL, no Dokany), with **server modes**
  `--serve mcp | dap | gdb` (MCP for AI/agent clients, Debug Adapter Protocol,
  and GDB Remote Serial Protocol).
- **TraceExport** — execution traces to JSON / CSV.
- **pybridge** — drive the debugger from Python, with headless templates.
- **ExtraCmds** plugin — `.init` / `.save` / `scylla_hide.*`, a minidump viewer,
  and the HvKdFusion commands.
- Bundled **ScyllaHide** and **xAnalyzer**; **OllyDumpEx** / **PE-sieve**
  installable with SHA-256 verification.

### Changed
- **Branding**: launcher renamed to `Fathom.exe` with its own icon; main-window
  title and version resource set to **Fathom**. The engine executables and DLLs
  keep their x64dbg names (plugins and the SDK depend on them).

### Fixed
- Minidump viewer build (`CMAKE_SYSTEM_VERSION`) on hosts reporting 6.2.9200.

### Credits
Fathom is a modified distribution of x64dbg; see [NOTICE](NOTICE) and
[CONTRIBUTING](CONTRIBUTING.md).

[1.1.0]: https://github.com/EGKrb/Fathom/releases/tag/v1.1.0
[1.0.0]: https://github.com/EGKrb/Fathom/releases/tag/v1.0.0
