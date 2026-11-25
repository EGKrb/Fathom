# Changelog

All notable changes to Fathom are documented here. Fathom is built on
[x64dbg](https://github.com/x64dbg/x64dbg) (GPL-3.0) and tracks its `development`
branch; this changelog covers Fathom's own additions and branding.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

## [1.8.0] - 2026-11-25

### Added
- Emulator: Direct3D 9 shaders run (vertex and pixel shaders of shader models
  1 to 3, with their float, int and bool constants), with the texture stages,
  alpha test, cube and volume textures, several render targets and queries.
- Emulator: every shader stage of Direct3D 10, 11 and 12 runs (DXBC up to
  shader model 5.1): hull shaders and the tessellator, domain and geometry
  shaders, compute shaders (`Dispatch`, thread groups, group shared memory,
  atomics, typed, raw and structured UAVs, append / consume counters).
- Emulator: Direct3D 10 (`D3D10CreateDevice(AndSwapChain)(1)`) and Direct3D 12
  (command queues and lists, root signatures, descriptor heaps, pipeline
  states, resources at their GPU virtual address, barriers, fences, copies with
  footprints, flip-model swap chains) draw and compute.
- Emulator: OpenGL beyond 1.1: the functions and extensions given by
  `wglGetProcAddress` (from `glext.h` and `wglext.h`), 3.x and 4.x contexts
  (`wglCreateContextAttribsARB`), GLSL vertex and fragment shaders (1.10 to
  4.60, in an interpreter), buffers, vertex array objects, framebuffer objects,
  1D / 3D / cube / array textures, mipmaps, S3TC, samplers, sRGB, syncs.
- Emulator: texture formats of DXGI and Direct3D 9 (8, 16 and 32-bit, float,
  integer, depth, sRGB, BC1 to BC5).
- Emulator: FMA3 instructions (`vfmadd`, `vfmsub`, `vfnmadd`, `vfnmsub`), which
  Unicorn lacks.
- `emulator_world` modes `clip`, `gl3`, `d3d9sh`, `d3d10`, `d3d11gs`,
  `d3d11cs`, `mesh` and `d3d12`, run natively and in the emulator by
  `extracmds/tests/test_emulator.py`.
- Emulator: shaders of shader model 6 (DXIL, compiled by dxc) run in Direct3D
  12: the LLVM bitcode is read and interpreted on all the lanes at once
  (resources, samplers, wave operations, atomics, group shared memory).
- Emulator: Direct3D 12 ray tracing (DXR 1.0): state objects with DXIL
  libraries and hit groups, shader identifiers and tables, bottom and top
  level acceleration structures, `DispatchRays`, `TraceRay` with closest hit,
  any hit (`IgnoreHit`, `AcceptHitAndEndSearch`) and miss shaders,
  `CallShader`, the ray system values.
- Emulator: GLSL geometry, tessellation and compute shaders
  (`glDispatchCompute`, shader storage blocks, shared memory, atomics,
  images, atomic counters).
- Emulator: stream output in Direct3D 10, 11 and 12 (its statistics queries
  and `DrawAuto`), deferred contexts and command lists in Direct3D 11, and
  Direct3D 10 effects (`D3D10CreateEffectFromMemory`, fx_4_0).
- Emulator: derivatives (`ddx` / `ddy` coarse and fine, `dsx` / `dsy`,
  `dFdx` / `dFdy`) and the mip level of textures, from pixels shaded in 2x2
  quads with helper lanes; `SampleBias`, `SampleGrad`, `CalculateLevelOfDetail`.
- Emulator: BC6H (unsigned and signed) and BC7 textures, decoded exactly as
  the GPU does.
- `emulator_world` modes `bc`, `deriv`, `tess`, `so`, `deferred`, `gl4`,
  `fx10`, `dxil` and `dxr`.

### Fixed
- Emulator: the tessellation follows the fixed tessellator of Direct3D 11 (a
  level per edge and one inside, the partitionings), in Direct3D and OpenGL.
- Emulator: VEX-encoded SSE instructions (AVX), which Unicorn ran as their
  legacy forms with a wrong first source (`printf("%g")` printed garbage with
  the FMA routines of the C runtime), are trapped and run right.
- Emulator: the rendering shares each batch of pixels between threads and the
  shader interpreter writes registers faster: about twice as fast (a mesh of
  20000 triangles: 0.7 s instead of 1.4 s). Runs of AVX instructions (`memcpy`,
  `memset`, auto-vectorized loops) are emulated in one trap, each instruction
  decoded once: the AVX code of a program runs about twice as fast.
- Emulator: a triangle that crosses the near plane (one vertex behind the
  camera) is clipped and drawn, in OpenGL and Direct3D.
- Emulator: the rasterizer draws a batch of triangles at once with numpy (top-left
  rule, perspective-correct): a mesh of 20000 triangles takes under a second
  instead of about 25.
- Emulator: a function of an emulated graphics DLL imported by ordinal
  (`D3D12CreateDevice` in `d3d12.lib`) is hooked.
- Emulator: `NtAreMappedFilesTheSame` is implemented, so `LoadLibrary` of a DLL
  already loaded under another path (`ntdll.dll` during the unloading at exit)
  finds it.

## [1.7.0] - 2026-11-22

### Added
- Emulator: GDI+ draws (its flat API implemented in Python, as `gdiplus.dll`
  needs the kernel side of GDI): graphics on DCs, windows and images, ARGB
  colors with their alpha, world transforms, solid and linear gradient brushes,
  pens, rectangles, ellipses, lines, polygons, arcs, Béziers, paths, text,
  bitmaps (`LockBits`, `HBITMAP` both ways, files loaded and saved with the
  encoders), `DrawImage`.
- Emulator: OpenGL 1.x draws (a software rasterizer): contexts and pixel
  formats, matrices (`glOrtho`, `glFrustum`, `gluPerspective`, `gluLookAt`...),
  `glBegin` / `glEnd` with every primitive, vertex arrays, 2D textures, depth
  test, culling, blending, flat or smooth shading, `glReadPixels`, the frame in
  the window at `SwapBuffers`.
- Emulator: Direct3D 9 draws: its fixed-function pipeline (FVF and vertex
  declarations, transforms, lights, a texture, depth test, culling, blending),
  `DrawPrimitive(UP)`, `DrawIndexedPrimitive(UP)`, vertex and index buffers,
  surfaces and textures (`LockRect`), `Clear`, `StretchRect`,
  `GetRenderTargetData`, `Present` to the window.
- Emulator: Direct3D 11 and DXGI draw: the vertex and pixel shaders (DXBC,
  shader models 4 and 5) run in an interpreter (`dxbc.py`) on every vertex and
  pixel at once; buffers, 2D textures and views, input layouts, constant
  buffers, samplers, rasterizer / blend / depth-stencil states, `Draw`
  (indexed, instanced), clears, copies, `Map`, the swap chain and `Present`.
- Emulator: `AlphaBlend` with an alpha per pixel (`AC_SRC_ALPHA`, a 32-bit
  premultiplied source).
- Emulator: processes that share memory interleave like threads on the
  emulated processors (a race between them on that memory happens as
  natively, interlocked instructions stay atomic).
- Emulator: window messages between processes: one set of windows
  (`FindWindow`, `FindWindowEx` with message-only windows,
  `GetWindowThreadProcessId`, `RegisterWindowMessage`, `HWND_BROADCAST`),
  `PostMessage` / `PostThreadMessage` to another process, `SendMessage` that
  waits for the receiving thread, `WM_COPYDATA` / `WM_SETTEXT` / `WM_GETTEXT`
  marshalled.
- Emulator: bitmaps (memory DCs, DIB sections in the program's memory,
  `BitBlt` / `StretchBlt` and raster operations, `StretchDIBits`,
  `SetDIBitsToDevice`, `Get/SetDIBits`, `TransparentBlt`, `AlphaBlend`,
  `LoadBitmap` / `LoadImage`), regions (`CombineRgn`, fills, clipping,
  `SetWindowRgn`) and paths (`BeginPath` ... `FillPath`, `SelectClipPath`,
  `PathToRegion`), `Polygon`, `PolyBezier`, `RoundRect`, `Arc`, `Pie`, `Chord`,
  `SaveDC` / `RestoreDC`, `GetPixel`.
- `emulator_world` modes `racep`, `winmsg`, `paint` (its pixels checked with
  `GetPixel` natively and in the emulator), `gdiplus`, `opengl`, `d3d9`, `d3d11`
  (their pixels read back natively and in the emulator, the images of their
  windows checked by the plugin test).

### Fixed
- Emulator: a `GetMessage` that waited for a timer did not wake for a posted
  message; the result of a run whose console output had NUL characters could
  not be read by ExtraCmds; on x64, the 32-bit arguments that a program passes
  on the stack had the garbage of the upper half of their slot in the emulated
  graphics functions.

## [1.6.0] - 2026-11-22

### Added
- Emulator: **several processors**. The emulated machine has the processors of
  the dumped one (`emu.cpus`, `--cpus`): the threads on processors run
  interleaved in short slices drawn from a fixed seed (`emu.seed`, `--seed`),
  so a race between threads happens as natively and the same run comes out
  every time.
- Emulator: **windows, network and child processes**. user32 windows,
  messages, timers, message boxes and dialogs in a headless window manager
  with a simulated user; Winsock, WinINet, WinHTTP and `URLDownloadToFile` on
  a simulated network (192.0.2.x addresses, HTTP 200 answers, everything
  recorded) or the host's (`emu.network real`, `--network real`);
  `CreateProcess` runs the child in an emulator of its own, and the parent's
  `VirtualAllocEx`, `WriteProcessMemory`, `ReadProcessMemory`,
  `Get/SetThreadContext`, `ResumeThread`, waits and `GetExitCodeProcess` act on
  it. The result of `fathom_emu.py` lists them (`gui`, `network`, `processes`).
  New in dumpulator: `apihooks.py`, `gui.py`, `network.py`, `processes.py`;
  `NtCompareObjects`, `NtFlushInstructionCache`.
- Test program `src/tests/emulator_world` (race, illegal instruction, windows,
  network, child processes), run by `extracmds/tests/test_emulator.py` in x64
  and x32.
- Emulator: **processes that run at the same time**. The emulated processes of
  a run take turns by slices and whenever one waits for another, with clocks
  that move together: pipes (anonymous on a child's redirected standard input
  and output, named, `PeekNamedPipe`, `TransactNamedPipe`), shared sections,
  named events / mutexes / semaphores, inherited and duplicated handles work
  between them as natively. Children keep running after their parent exits.
- Emulator: **children of the other architecture**, started from a dump of
  `emulator_host` of that architecture (made once by Fathom's headless of that
  architecture, shipped in `emulator\hosts`).
- Emulator: **COM, ShellExecute, RPC, services**. In-process COM objects run in
  the emulator (`CoCreateInstance`, `CoGetClassObject`: their DLL loaded,
  `DllGetClassObject` and `IClassFactory::CreateInstance` called);
  out-of-process servers and RPC to other processes are answered as
  unavailable and recorded (CLSID, interface, procedure); `ShellExecute(Ex)`
  runs programs and batch files in the emulator (documents and URLs are
  recorded); the service manager is emulated.
- Emulator: **windows drawn**. `WM_PAINT` and gdi32 (text, colors, brushes,
  pens, shapes, controls) are drawn into images; `emu.screenshots on` /
  `--screenshots DIR` save a PNG of each window and message box.
- Emulator: **registry and files from the host**, read-only: the program's
  writes stay in the emulator (`--files-out DIR` saves the files it wrote).
  `LoadLibrary` loads any DLL from the disk with its dependencies, TLS and
  `DllMain`.
- `emu.screenshots`; `emulator_world` modes `pipe`, `shm`, `namedpipe`,
  `cross`, `shell`, `com`, `system`; `src/tests/emulator_host`.

### Changed
- Emulator: what it lacks no longer stops it. A system call it does not
  implement returns `STATUS_NOT_IMPLEMENTED` to the program, an instruction it
  does not emulate raises `STATUS_ILLEGAL_INSTRUCTION` in it; both are named in
  the summary. `emu.strict on` (`--strict`) stops at them as before.

### Fixed
- Emulator: `emu.run 0, n` (`--count`) runs exactly `n` instructions with
  several threads (300,409 recorded for 300,000 asked before).
- Emulator: a 32-bit program whose stack grows through its guard page
  (`_chkstk`) crashed unicorn; the stack of the dumped thread is now committed
  when the dump is loaded.
- Emulator: `NtProtectVirtualMemory` on a range that starts inside a page (an
  import table across two pages stayed read-only), `NtAllocateVirtualMemory`
  on unaligned ranges and committed pages, `dp.call` without an aligned x64
  frame, an exception frame not 16-byte aligned, the loader's TLS for a DLL
  loaded late, `BeingDebugged` of the dump's debugger, a dump segment past its
  memory information (a dump made in the GUI could not be loaded).
- Emulator: loading a DLL or a child process maps runs of pages at once (a
  child that loaded DLLs took minutes).

## [1.5.0] - 2026-11-20

### Added
- **Emulator** (`emu.*` commands of ExtraCmds, `emulator/`): code of the
  debuggee run in [dumpulator](https://github.com/mrexodia/dumpulator) from a
  minidump of its current state, with the Windows system calls and exceptions
  implemented in Python; the debuggee does not move. `emu.call address[, args]`
  calls a function (`str:` / `wstr:` / `buf:` / `bytes:` arguments, `$result` =
  return value), `emu.run [end[, count]]` runs the current thread on,
  `emu.script file.py` runs a script with dumpulator's API, `emu.load file.dmp`
  emulates any minidump, even without debugging. `emu.trace on` records the
  emulated instructions in x64dbg's trace format and opens them in the Time
  Travel tab (about 40,000 instructions per second). pybridge:
  `Debugger.emulate_call()`, `emulate_run()`, `minidump()` and `emulator()` (a
  `Dumpulator` of the debuggee in the client). dumpulator is vendored with
  changes for unicorn 2.1 and Python 3.12+, `NtOpenThreadToken[Ex]`, and the
  AVX/AVX2 instructions of the C runtime emulated in Python (checked against the
  CPU by `emulator/tests/test_avx.py`). Test: `extracmds/tests/test_emulator.py`.
- Emulator: **several threads and whole programs**. Threads the emulated code
  creates start like on Windows (TLS, `DLL_THREAD_ATTACH`) and take turns
  (waits, every 20,000 instructions): events, mutexes, semaphores, threads,
  alerts by thread id (critical sections, SRW locks, condition variables),
  keyed events, `Sleep` and timeouts on a virtual clock that
  KUSER_SHARED_DATA follows; deadlocks are reported. A program runs from its
  entry point to its exit: DLLs from `\KnownDlls`, console output,
  `RaiseException`, tokens, registry lookups, WNF states, recent information
  classes, `monitorx` / `mwaitx`. The test program `src/tests/emulator_threads`
  gives the same result emulated as native (x64 and x86); recorded, its trace
  has its five threads in Time Travel and the Trace tab.
- ExtraCmds: `hvkd.log` writes the output of HvKdFusion to the log as it comes,
  not at the end.

### Changed
- `TraceSetThreads all` with `TraceSetSkip`: a thread created during the trace
  runs its end at full speed once it returns from its start function into a
  system module (`DLL_THREAD_DETACH` notifications, `RtlExitUserThread`). In
  32-bit, a WoW64 thread in its 64-bit part (outside every module) is no longer
  taken for a thread back in traced code. The `trace_all_threads` test records
  about 2,200 instructions instead of 50,000 to 70,000 (3 s instead of 30 s).

## [1.4.1] - 2026-11-05

### Fixed
- `TraceSetThreads all` with the GleeBug debug engine: its TitanEngine emulation
  has no `StepIntoThreads` / `StepCancelThreads`, and the first call crashed
  (delay-loaded import). The command now sets `$result` to 0, says so and traces
  the current thread; the test `trace_all_threads` skips on that engine.

## [1.4.0] - 2026-11-05

### Added
- **`TraceSetSkip none|system|only[, modules]`**: the calls into modules that are
  not traced (the system modules, or every module but the listed ones) run at
  full speed and are recorded as one instruction, like a step over. The
  destination is resolved through the import thunks; code outside any module is
  always traced. On a loop calling `wsprintfA`, `lstrlenA` and `GetTickCount64`
  then `qsort`, recording the function goes from 255,571 to 69,711 instructions
  and from 20.3 s to 5.5 s. A skipped call is marked in the trace (user block
  `0x83`) and in the JSON export of `TraceExport` (`skip` member).
- **`TraceSetThreads current|all`**: with `all`, every thread of the debuggee is
  single-stepped (TitanEngine `StepIntoThreads` / `StepCancelThreads`), including
  the threads created during the trace, and the trace holds the instructions of
  all of them in the order of their debug events instead of following one thread.
  Combined with `TraceSetSkip`, the loader initialization of a new thread runs at
  full speed up to its start address. Test `trace_all_threads`: three workers
  incrementing a shared counter with an interlocked instruction, every increment
  recorded.

### Changed
- **Recorder**: an instruction is written when its own thread reports the next
  one (once it executed), not at the next event of any thread, so the memory
  written by an instruction of one thread is no longer read before it ran. The
  file format is unchanged; a thread reported twice at the same instruction
  without executing it gets one block.
- **Recording about 12% faster**: while trace recording is on, TitanEngine reads
  the AVX registers with the rest of the thread context in one system call per
  instruction (engine variable `UE_ENGINE_CONTEXT_CACHE_AVX`), and the recorder
  reuses the instruction bytes it already read.

## [1.3.0] - 2026-11-05

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
- pybridge: the docstring of `export_trace()` documents `$result1` / `$result2`.
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

[1.8.0]: https://github.com/EGKrb/Fathom/releases/tag/v1.8.0
[1.7.0]: https://github.com/EGKrb/Fathom/releases/tag/v1.7.0
[1.6.0]: https://github.com/EGKrb/Fathom/releases/tag/v1.6.0
[1.5.0]: https://github.com/EGKrb/Fathom/releases/tag/v1.5.0
[1.4.1]: https://github.com/EGKrb/Fathom/releases/tag/v1.4.1
[1.4.0]: https://github.com/EGKrb/Fathom/releases/tag/v1.4.0
[1.3.0]: https://github.com/EGKrb/Fathom/releases/tag/v1.3.0
[1.2.0]: https://github.com/EGKrb/Fathom/releases/tag/v1.2.0
[1.1.0]: https://github.com/EGKrb/Fathom/releases/tag/v1.1.0
[1.0.0]: https://github.com/EGKrb/Fathom/releases/tag/v1.0.0
