# Fathom emulator (dumpulator)

Run code of a Windows process **in an emulator**, from a minidump: the memory, modules and thread of
the process are loaded in [Unicorn](https://www.unicorn-engine.org/), and what the code asks of
Windows — the **system calls** (memory, files, registry, events, sections, threads...) and the
**exceptions** (SEH, VEH, unhandled exception filter) — is implemented in Python. A function of the
process can be called with arguments of your choice, or the program run on, **with its threads**,
up to its exit; the real process is never touched.

It is [dumpulator](https://github.com/mrexodia/dumpulator) by Duncan Ogilvie (mrexodia, the author
of x64dbg), vendored here with the changes listed below, plus `fathom_emu.py`: a command line that
Fathom's `emu.*` commands and pybridge use, and that records the emulated code in x64dbg's trace
format.

## In Fathom

The ExtraCmds plugin saves the debuggee to a minidump when needed (kept until the debuggee runs
again) and runs the emulator on it; see the "Emulator" section of the [README](../README.md):

```
emu.call ntdll.RtlComputeCrc32, 0, str:hello, 5     // $result = 0x3610A686
emu.call mymodule.decrypt, buf:256, 0x140017000     // the buffer is shown after the call
emu.trace on                                        // the next emulations open in Time Travel
emu.run 0, .10000                                   // the current thread, 10,000 instructions on
emu.script "C:\scripts\decrypt.py", 0x140001000     // dumpulator's API from Python
emu.load "C:\dumps\other.dmp"                       // any minidump, even without debugging
```

From pybridge: `dbg.emulate_call("kernel32.lstrlenW", "wstr:text")`, `dbg.emulate_run(...)`, and
`dbg.emulator()`, which returns a `Dumpulator` of the debuggee in the client's Python process.

## Command line

```
python fathom_emu.py info   app.dmp
python fathom_emu.py call   app.dmp 0x140001000 0x1234 str:hello buf:256
python fathom_emu.py call   app.dmp kernel32!lstrlenA str:hello --trace out.trace64
python fathom_emu.py run    app.dmp --end 0x140001234 --count 100000 --syscalls
python fathom_emu.py script app.dmp examples\crc_strings.py ntdll!RtlComputeCrc32
python fathom_emu.py setup                         # pip install -r requirements.txt
```

| Option | |
|---|---|
| `call DUMP ADDRESS [ARG ...]` | calls a function. `ADDRESS`: `0x...` or `module!export`. Arguments: integers, `str:TEXT` (ANSI string allocated in the emulator), `wstr:TEXT` (UTF-16), `buf:SIZE` (zeroed buffer, shown after the call), `bytes:HEX` |
| `run DUMP [--begin A] [--end A]` | runs the dumped thread from its instruction pointer (or `--begin`) |
| `script DUMP FILE [ARG ...]` | runs a Python script with `dp` (the `Dumpulator`), `args` and `result` |
| `--thread TID` | the thread to emulate (default: the thread of the dump's exception record, else the first) |
| `--count N` | stops after N instructions |
| `--trace FILE` | records the instructions to an x64dbg trace (`.trace64`, or `.trace32` for a 32-bit process) |
| `--syscalls` | shows the system calls and exceptions |
| `--strict` | stops at a system call or an instruction the emulator lacks (default: it fails in the program, see below) |
| `--cpus N` | processors of the emulated machine (default: those of the dumped machine, `PEB.NumberOfProcessors`) |
| `--seed N` | seed of the interleaving of the threads (default 1: the same run every time) |
| `--network simulated\|real` | the network of the program (default: simulated); `real` goes through the host, only for a program you trust |
| `--http-body FILE` | the body of the simulated HTTP responses |
| `--child-limit N` | instructions a child process runs at most (default 50,000,000) |
| `--screenshots DIR` | saves images (PNG) of the windows of the program and of its message boxes in DIR |
| `--files-out DIR` | saves the files the program wrote (they stay in the emulator otherwise) in DIR |
| `--text-trace` | also writes dumpulator's own text trace (`<dump>.trace`) |
| `--result FILE` | writes the result as JSON (return value, final address, buffers, trace, exit code) |

The dump must contain the full memory of the process: Fathom's `minidump` command (or `.save
file.dmp`), Task Manager's "Create dump file", `procdump -ma`. Python 3.9 or later with the packages
of `requirements.txt` (unicorn 2.1, capstone 5, pefile; Pillow for the images of the windows);
tested with Python 3.14.

## Traces

`TraceWriter` (in `fathom_emu.py`) hooks every emulated instruction and writes a block in the format
of x64dbg's recorder ([`docs/developers/tracefile.md`](../docs/developers/tracefile.md)): the
registers before the instruction (x64dbg's `REGDUMP`: general, segment, debug, MXCSR, XMM and YMM
registers and the decoded flags, those that changed since the previous block and all of them every
512 instructions; the x87 registers stay at zero) and the memory the instruction read or wrote, one pointer-sized word per access, read
before and after it. So `TraceExport` converts the trace to JSON, CSV, Tenet, Frinet or `.ftt`, and
the Time Travel tab and x64dbg's trace view open it. The registers are read with one call to
unicorn's library per instruction: about 40,000 instructions per second while recording.

Not in the trace: instructions in the kernel half of the address space (dumpulator returns from
exceptions through a small stub there) and, for a 32-bit process, the 64-bit code; what the system
calls write to memory (they are Python code), only their result in the registers. When an
instruction faults, it is written once, followed by the exception dispatcher.

From Python:

```python
from dumpulator import Dumpulator
from fathom_emu import TraceWriter

dp = Dumpulator("app.dmp", quiet=True)
writer = TraceWriter(dp, "out.trace64")
dp.call(0x140001000, [0x1234])
print(writer.close(), "instructions")
```

## Threads and whole programs

Several threads run, on several emulated processors (`dumpulator/threads.py`):

- A thread the emulated code creates (`CreateThread`, `_beginthreadex`: `NtCreateThreadEx`) gets a
  stack, a TEB and an initial context that enters `ntdll!LdrInitializeThunk`, as Windows starts a
  thread: the loader gives it its static TLS and calls `DLL_THREAD_ATTACH`, then
  `RtlUserThreadStart` calls the thread's function; when it returns, `DLL_THREAD_DETACH` and the
  exit code. Suspended threads, `ResumeThread` / `SuspendThread`, `TerminateThread`.
- The emulated machine has the processors of the dumped one (`--cpus` to choose): up to that many
  threads are on a processor at once and run interleaved in slices of 40 to 400 instructions, of a
  length drawn from a fixed seed (`--seed`), as the instructions of threads mix on a multiprocessor:
  a race between two threads happens as it does natively (the `race` mode of
  `src/tests/emulator_world` loses updates of an unprotected counter, never those of an interlocked
  one), and the same seed gives the same run. A thread leaves its processor when it waits for
  something that is not signaled, and after 20,000 instructions of its own when another thread waits
  for a processor. The virtual clock counts the time of one processor. Waits: events, mutexes (recursive, abandoned), semaphores, threads,
  `WaitForMultipleObjects` (any / all), `SignalObjectAndWait`, the alerts by thread id of critical
  sections, SRW locks, condition variables and `WaitOnAddress`, keyed events, `Sleep`,
  `SwitchToThread`, timeouts.
- Time is virtual: it advances with the emulated instructions (one per nanosecond) and jumps over
  sleeps and timeouts when every thread waits; `GetTickCount`, `QueryPerformanceCounter` and the
  system time follow it (KUSER_SHARED_DATA). When every thread waits without a timeout, the
  emulation stops and says which threads wait for what (deadlock).
- The other threads of the dump stay where they are: they wait in the kernel.

A program runs from its entry point to its exit, through the start-up and the end of the C
runtime: the DLLs it loads (`LoadLibrary`, through `\KnownDlls` or from the disk, with their
dependencies, their TLS and their `DllMain`), the console (`WriteConsole`, `printf`; the text is in the
output and in the `console` member of the result), `RaiseException` (`OutputDebugString` included)
dispatched to the program's handlers, `ExitProcess` ending the other threads before the DLLs are
unloaded. The access token is that of a standard user, not elevated, medium integrity. The emulated
process is not debugged (`BeingDebugged` and the heap flags of the dump's debugger are cleared).

**Registry and files** (`registry.py`, `filesystem.py`): what the program reads comes from the host,
read-only (the WoW64 views of the registry and `SysWOW64` for a 32-bit program); what it writes,
creates, renames or deletes stays in the emulator, in an overlay shared by the processes of the run.
The keys and files changed are in the result (`registry`, `files`; `--files-out` saves the files
written), and the host never changes. Files: `CreateFile` with every disposition, read, write,
append, position, size, attributes and times, rename, delete (on close too), directories and their
listing (`FindFirstFile`: `NtQueryDirectoryFile[Ex]` with patterns), volume information, sections of
files. Registry: `RegCreateKeyEx`, `RegOpenKeyEx`, `RegQueryValueEx`, `RegSetValueEx`,
`RegEnumKeyEx`, `RegEnumValue`, `RegQueryInfoKey`, `RegDeleteValue`, `RegDeleteKey`, notifications
(they stay pending).

`src/tests/emulator_threads` is the reference: four workers (one created suspended) contend for a
critical section, count with interlocked instructions, wait for an event, a mutex and a semaphore,
signal a condition variable under an SRW lock and use static TLS, while the main thread sleeps and
waits for them; it prints its checks and exits with their number of failures. Natively and in the
emulator (x64 and x86), it prints `12000 increments, total 30000, 0 failure(s)` and exits with 0;
recorded, its 1.5 million instructions make a trace with five threads that the Time Travel tab
shows (`extracmds/tests/test_emulator.py`).

`--count N` runs exactly N instructions, whatever the number of threads: with a count, every
instruction is counted as it runs (a slice cut short by a thread switch, an exception or an
instruction emulated in Python included).

## What the emulator lacks

A system call the emulator does not implement returns `STATUS_NOT_IMPLEMENTED` to the program, as on
a Windows version without it, and an instruction unicorn and `avx.py` do not emulate raises
`STATUS_ILLEGAL_INSTRUCTION` in the program (through `KiUserExceptionDispatcher`, so its handlers
see it), as on a processor without it: most programs take another path, and the emulation goes on.
Each one is named once in the output, and the summary at the end counts them (`not_implemented` in
the result). `--strict` (`emu.strict on` in Fathom) stops the emulation at them instead, as
dumpulator does.

## Outside the process: windows, network, other processes, COM

Functions of DLLs are replaced by Python code where the emulator implements a part of Windows that
is above the system calls (`dumpulator/apihooks.py`: a handler gets the arguments of the function
and returns its result, or calls emulated code first, such as a window procedure). The events are in
the output and in the result (`gui`, `network`, `processes`, `com`, `shared_objects`).

- **Windows** (`gui.py`): a window manager without a display. Window classes, windows
  (`CreateWindowEx` sends `WM_NCCREATE` / `WM_CREATE` to their procedure), messages
  (`GetMessage`, `PeekMessage`, `DispatchMessage`, `SendMessage`, `PostMessage`,
  `PostQuitMessage`), timers on the virtual clock (`SetTimer`: `WM_TIMER` in the queue, `GetMessage`
  sleeps until the next one), window text and properties, message boxes and dialogs
  (`DialogBoxParam`: `WM_INITDIALOG`, then `IDOK`). A simulated user answers message boxes and
  dialogs with OK and closes the windows when the program waits with nothing to do, so a program
  with a message loop reaches its end. What the windows show is **drawn** (`gui_draw.py`, with
  Pillow): `ShowWindow` and `InvalidateRect` queue `WM_PAINT`, `UpdateWindow` sends it, and the
  program paints as natively through `BeginPaint` / `GetDC` and gdi32 (text with its colors and
  background mode, solid brushes and pens, stock objects, `Rectangle`, `Ellipse`, `MoveToEx` /
  `LineTo`, `SetPixel`, `FillRect` / `FrameRect`, `DrawText` with its alignment,
  `GetTextExtentPoint32`); buttons, labels and edit fields are drawn with their text, message boxes
  as dialogs with their buttons. `--screenshots DIR` (`emu.screenshots on` in Fathom) saves an image
  of each window (when it is destroyed, or at the end) and of each message box; the text drawn is in
  the events too. Bitmaps: memory DCs, `CreateCompatibleBitmap`, `CreateBitmap`, `CreateDIBitmap`,
  DIB sections (their pixels are the program's memory, read and written there), `BitBlt` /
  `StretchBlt` with the usual raster operations, `PatBlt`, `StretchDIBits`, `SetDIBitsToDevice`,
  `Get/SetDIBits`, `TransparentBlt`, `AlphaBlend` (constant alpha and alpha per pixel), `LoadBitmap` / `LoadImage` (from
  the resources or a file), `GetObject`, `GetPixel`. Regions: rectangles, ellipses, rounded
  rectangles, polygons, `CombineRgn`, `OffsetRgn`, `GetRgnBox`, `PtInRegion`, `Fill` / `Paint` /
  `Frame` / `InvertRgn`, clipping (`SelectClipRgn`, `ExtSelectClipRgn`, `IntersectClipRect`,
  `ExcludeClipRect`), `SetWindowRgn` (the window's shape in its image). Paths: `BeginPath` ...
  `EndPath` around lines, Béziers, arcs and shapes, `StrokePath`, `FillPath`, `StrokeAndFillPath`
  (`SetPolyFillMode`), `SelectClipPath`, `PathToRegion`. Shapes: `Polygon`, `Polyline`, `PolyBezier`,
  `RoundRect`, `Arc`, `Pie`, `Chord`; `SaveDC` / `RestoreDC`.
- **GDI+, OpenGL, Direct3D** (with numpy): `gdiplus.dll`, `opengl32.dll`, `d3d9.dll`, `d3d10.dll`,
  `d3d11.dll`, `d3d12.dll` and `dxgi.dll` need the kernel side of GDI or a graphics driver, so their
  functions are implemented in Python and draw into the windows' images (their `DllMain` is not
  run). GDI+ (`gdiplus_emu.py`, its flat API): graphics on DCs, windows and images, ARGB colors with
  their alpha, transforms, clip, solid and linear gradient brushes, pens, shapes, Béziers, paths,
  text, bitmaps (`LockBits`, `HBITMAP` both ways, files with the encoders), `DrawImage`.
  OpenGL 1.x (`opengl_emu.py`): `wgl*` contexts, pixel formats, `SwapBuffers`, matrices and `glu`
  helpers, `glBegin` / `glEnd` with every primitive, vertex arrays, 2D textures, depth test,
  culling, blending, shading, `glReadPixels`. OpenGL 2.0 to 4.x (`gl_emu.py`): the functions and
  extensions that `wglGetProcAddress` gives (the 2649 of `glext.h` and the 116 of `wglext.h`, with
  their parameter types; those not implemented are named in the summary), `wglCreateContextAttribsARB`
  (core and compatibility profiles), buffers (`glMapBufferRange`, uniform buffers in std140),
  vertex array objects, instancing, 1D / 2D / 3D / cube / array textures with mipmaps and S3TC,
  samplers, framebuffer objects and renderbuffers, `glBlitFramebuffer`, sRGB, queries and syncs;
  the GLSL shaders of every stage (1.10 to 4.60, `glsl.py`: preprocessor, parser and an
  interpreter that runs every vertex of a draw and every fragment of a batch at once): vertex,
  tessellation control and evaluation, geometry (`EmitVertex`, several streams), fragment and
  compute (`glDispatchCompute`, shared memory, barriers, atomics), with shader storage and uniform
  blocks, images (`imageLoad` / `imageStore` / atomics), atomic counters and `dFdx` / `dFdy`.
  Direct3D 9 (`d3d9_emu.py`): the fixed-function pipeline (FVF and vertex declarations, transforms,
  viewport, lights, the texture stages, alpha test, depth, culling, blending, wireframe) and the
  vertex and pixel shaders of shader models 1 to 3 (`d3d9_shader.py`, with their float, int and
  bool constants), `DrawPrimitive(UP)`, `DrawIndexedPrimitive(UP)`, buffers, surfaces, 2D, cube and
  volume textures, several render targets, queries, `Clear`, `ColorFill`, `StretchRect`,
  `GetRenderTargetData`, `Present`. Direct3D 10 (`d3d10_emu.py`), 11 (`d3d11_emu.py`) and DXGI
  (`dxgi_emu.py`, factories 1 to 6, adapters, outputs, swap chains with the flip model): devices,
  buffers, 1D / 2D / 3D textures and their views (shader resource, render target, depth stencil,
  unordered access), input layouts, constant buffers, samplers, the fixed states, `Draw` (indexed,
  instanced, indirect), `Dispatch`, clears, copies, `UpdateSubresource`, `Map`. Direct3D 12
  (`d3d12_emu.py`): devices, command queues, allocators and lists (recorded, run at
  `ExecuteCommandLists`), root signatures (serialized and parsed), descriptor heaps, pipeline
  states, committed and placed resources at their GPU virtual address, barriers, fences with their
  events, `CopyTextureRegion` with footprints, `GetCopyableFootprints`. The shaders of Direct3D 10,
  11 and 12 (DXBC, shader models 4.0 to 5.1) run in an interpreter (`dxbc.py`, pipeline in
  `d3d_pipeline.py`) at every stage: vertex, hull (its phases) and the tessellator of Direct3D 11
  (a level per edge and inside, integer, pow2 and fractional odd / even partitionings, the rings
  stitched), domain, geometry (emitted strips, streams), pixel (several targets, discard, depth
  output), compute (thread groups, group shared memory, barriers, atomics, typed, raw and
  structured UAVs, append / consume counters). The shaders of shader model 6 (DXIL, made by dxc)
  run in `dxil.py`: the LLVM 3.7 bitcode is read by `dxil_bitcode.py` (types, constants, metadata,
  functions) and run on all the lanes at once (blocks in reverse post-order with lane masks), with
  the `dx.op` operations of the resources, samplers, derivatives, wave operations and atomics.
  Stream output (`SOSetTargets`, its statistics and `DrawAuto`), deferred contexts and command
  lists (`d3d11_emu.py`), Direct3D 10 effects (`D3D10CreateEffectFromMemory`, `d3d10_effect.py`: the
  fx_4_0 binary, variables, techniques, passes and their states) and the ray tracing of Direct3D 12
  (`dxr.py`: state objects, acceleration structures with their instances, `DispatchRays`, any hit,
  closest hit and miss shaders, `TraceRay` and the ray system values) are emulated. The pixel
  shaders run in 2x2 quads with helper lanes when they need derivatives (`ddx` / `ddy` coarse and
  fine, `dsx` / `dsy`, `dFdx` / `dFdy`) or the mip level of a texture. Texture formats (`gpu.py`,
  `bc67.py`): the DXGI and Direct3D 9 formats, 8, 16 and 32-bit, float and integer, depth, sRGB and
  BC1 to BC7 (BC6H and BC7 decoded as the GPU does, checked against it). The triangles (`raster.py`)
  are clipped against the near and far planes, then rasterized with numpy at the pixel centers
  with the top-left rule, perspective-correct, in batches of about a million pixels; the coverage
  and, when the shader has no side effect, the fragment shading of a batch are shared between
  threads (4, `FATHOM_RASTER_THREADS`), and the depth test, blending, write masks and multiple
  targets are merged in order (a mesh of 20000 triangles: 0.7 s, against 25 s one triangle at a
  time). The COM objects are vtables of stubs whose methods are
  Python (`apihooks.ComRuntime`), generated with the parameter types of the SDK's headers
  (`tools/make_graphics_tables.py` writes `graphics_tables.py`). The functions of these DLLs are
  hooked only when a module imports them (by name or by ordinal) or `GetProcAddress` asks for
  them, so a program that does not use them runs as fast as before.
- **Window messages between processes**: the windows of the processes of a run are one set
  (`FindWindow`, `FindWindowEx` with message-only windows, `GetWindowThreadProcessId`,
  `RegisterWindowMessage`, `HWND_BROADCAST`). A message posted to a window or a thread of another
  process goes to that thread's queue; a message sent to it (`SendMessage`, `SendMessageTimeout`)
  waits until the receiving thread handles it in its `GetMessage` / `PeekMessage`, as natively;
  `WM_COPYDATA`, `WM_SETTEXT` and `WM_GETTEXT` carry their data from one process to the other. A
  `GetMessage` that waits for a timer also wakes for a message, and one with nothing to do waits for
  the other processes before the simulated user closes the windows.
- **Network** (`network.py`): Winsock (`socket`, `connect`, `send`, `recv`, `WSASend`/`WSARecv`,
  `sendto`/`recvfrom`, `getaddrinfo`, `gethostbyname`, `select`...), WinINet (`InternetOpen`,
  `InternetConnect`, `HttpOpenRequest`, `HttpSendRequest`, `InternetOpenUrl`, `InternetReadFile`,
  `HttpQueryInfo`), WinHTTP and `URLDownloadToFile`. Simulated by default: a name resolves to an
  address of 192.0.2.0/24 (one per name, the documentation range), connections succeed, what the
  program sends is recorded, an HTTP request gets `200 OK` with a body of your choice
  (`--http-body`), a datagram gets no answer. `--network real` sends everything through the host
  (Python's `socket` and `urllib`): only for a program you trust.
- **Child processes** (`processes.py`): `CreateProcess` (all its variants end in
  `kernelbase!CreateProcessInternalW`) and `ShellExecute(Ex)` of a program or a batch file load a
  second `Dumpulator`: from the same dump (the same system DLLs, already initialized) or, for a child
  of the **other architecture**, from a dump of `emulator_host` of that architecture (a small program
  in `hosts\`, started once by Fathom's `headless.exe` of that architecture and saved at its entry
  point, kept in `%TEMP%\Fathom\emu`: only the system's loader runs natively). The child's executable
  is mapped from the disk, its imports bound (API sets through the `ApiSetMap` of the PEB; a DLL the
  dump lacks is loaded by the child's own loader, `DllMain` included), its command line (process
  parameters, kernelbase, ucrtbase), image path, process and thread ids set, and its main thread
  starts at `RtlUserThreadStart(entry point, PEB)`. A document or a URL given to `ShellExecute` is
  recorded (its program is not started).
- **Processes that run at the same time** (`processes.py`: the World of the run): when processes
  share memory (a section mapped in two of them), their instructions interleave on the emulated
  processors like those of threads (slices of 40 to 400 instructions drawn from the seed): a race
  between them on that memory happens as natively, and an interlocked instruction stays atomic (one
  process runs an instruction at a time). Processes that share no memory run by slices of 20,000
  instructions (they can only see each other through system calls). When one waits for something
  another may do (a child's exit, a pipe, an event, a message), the others run until it can go on;
  their virtual clocks move together and jump to the first timeout only when every process waits. A child
  keeps running after its parent has exited (at most `--child-limit` instructions). Handles marked
  inheritable (`OBJ_INHERIT`, `SetHandleInformation`) are inherited, and the standard handles of
  `STARTUPINFO` (`STARTF_USESTDHANDLES`) become the child's: a pipe to its input or output works as
  natively. The parent's handles of the child work: `VirtualAllocEx`, `VirtualFreeEx`,
  `VirtualProtectEx`, `VirtualQueryEx`, `WriteProcessMemory`, `ReadProcessMemory`,
  `Get/SetThreadContext` (any thread), `Resume/SuspendThread`, `TerminateProcess`,
  `NtMapViewOfSection` (an image: process hollowing) and `NtUnmapViewOfSection`,
  `CreateRemoteThread`, `DuplicateHandle` into and out of the child, the waits,
  `GetExitCodeProcess`. Its console is its parent's; its own events are in the result under its
  entry.
- **What processes share** (`ipc.py`): named events, mutexes, semaphores and sections are the same
  objects in every process of the run (`CreateEvent` of an existing name opens it); a section
  (`CreateFileMapping`, `MapViewOfFile`) is host memory mapped into each emulator that maps it, so its
  bytes are shared as natively; pipes, anonymous (`CreatePipe`) and named (`CreateNamedPipe`,
  `ConnectNamedPipe`, `CreateFile` of `\\.\pipe\name`, `PeekNamedPipe`, `TransactNamedPipe`,
  message mode), between threads and processes: a read of an empty pipe waits for data, and gets
  `ERROR_BROKEN_PIPE` once every handle of the other end is closed.
- **COM, RPC, services** (`com.py`): an in-process COM server runs in the emulator:
  `CoCreateInstance(Ex)` / `CoGetClassObject` read the class's `InprocServer32` in the registry, load
  the DLL (`DllMain` runs) and call its `DllGetClassObject` and `IClassFactory::CreateInstance`, so
  the object's own code runs (msxml6 parses XML in the `com` mode of `emulator_world`). A class served
  by another process gets `CO_E_SERVER_EXEC_FAILURE`, an RPC to another process (`NdrClientCall2/3/4`)
  `RPC_S_SERVER_UNAVAILABLE`, as when the server does not answer; both are recorded with the CLSID,
  or the interface and the procedure number. The service manager is emulated: `CreateService`,
  `OpenService`, `StartService`, `ControlService`, `QueryServiceStatus(Ex)`, `ChangeServiceConfig`,
  `DeleteService`; the services created are keys of the emulator's registry.

## Changes to dumpulator

dumpulator (v1.0.0, commit `18a64d7`) is in `dumpulator/` and its patched copy of skelsec's
`minidump` library in `minidump/` (unchanged). Fathom's additions are in their own files, so that
the rest stays close to dumpulator:

- `threads.py` (new): the threads, the processors, the synchronization objects, the virtual clock.
- `apihooks.py`, `gui.py`, `gui_draw.py`, `network.py`, `processes.py`, `ipc.py`, `com.py` (new):
  functions of DLLs in Python, the windows and their images, the network, the processes of a run and
  what they share, COM, RPC and the service manager.
- `registry.py`, `filesystem.py` (new): the registry and the files, read from the host, changed in
  the emulator.
- `fathom_nt.py` (new): the system calls added or completed (they replace the stubs of
  `ntsyscalls.py` by name): sections of `\KnownDlls` and their mapping, image and recent memory
  information classes, threads, waits, events, mutants, semaphores, keyed events, alerts by thread
  id, time, the console, tokens, the registry, process cookie, Windows notification facility states,
  `NtRaiseException`, `NtManageHotPatch`, the TLS of a DLL loaded late (`ProcessTlsInformation`),
  `NtAllocateVirtualMemory` / `NtProtectVirtualMemory` on unaligned ranges, system and token
  information. Information classes are read as integers: recent Windows versions have classes
  dumpulator's enumerations lack.
- `vex.py` (new): unicorn runs a VEX.128 instruction it knows as the legacy SSE instruction of the
  same opcode, ignoring its second source (`vsubsd xmm4, xmm0, xmm1` gives `xmm4 - xmm1`): the VEX
  instructions are found in each block translated (a hook on the generated blocks, a byte filter
  first) and replaced by `ud2`; the trap runs the original instruction (encoded again as SSE on a
  small unicorn of its own, or by `avx.py`), and the AVX instructions that follow it at once.
  `FATHOM_VEX=0` turns it off.
- `avx.py` (new): **AVX and AVX2 instructions in Python**. Unicorn has no AVX, but the C runtime of a
  process that ran on a recent CPU chose its AVX2 routines at start-up (`wcslen`, `memcpy`,
  `memchr`...): the logical, comparison, minimum / maximum, add / subtract, mask, test, broadcast,
  shuffle, align, permute, insert / extract, unpack and byte-shift forms on `xmm` and `ymm` registers
  are emulated when unicorn rejects them, and `monitor` / `mwait` (`monitorx` / `mwaitx` on AMD) of
  spin loops are waits that end at once. `tests/test_avx.py` runs each AVX instruction natively and
  emulated on random values and compares the results (`python tests/test_avx.py`, needs a CPU with
  AVX2).
- `dumpulator.py`: unicorn 2.1 (the virtual TLB mode, `ctl_set_tlb_mode(UC_TLB_VIRTUAL)`, without
  which unicorn 2.1 cannot reach the GDT and the code stub dumpulator puts in the kernel half of the
  address space; `hook_add` with positional arguments, `arg1` became `aux1`; `ctl_request_cache`
  returns a tuple), the scheduler in `start()` (time slices, the instruction count kept across
  threads), the thread number in the system call log, its messages in its log instead of the output;
  `call()` gives the function an aligned x64 frame (home space, `rsp` = 8 modulo 16); the exception
  frame is 16-byte aligned; runs of pages are mapped at once (`memory.py` too: loading a DLL is much
  faster); a dump segment that goes past its memory information no longer stops the loading.
- `ntprimitives.py`: the type annotations of a structure are read from its class (Python 3.14 no
  longer finds them on an instance). `ntsyscalls.py`: messages in the log.

With these changes, dumpulator's own tests (its `Tests` DLL on the `HarnessMinimal` dumps, x64 and
x86: handles, files, memory) pass, as does its exception test (SEH, VEH, vectored continue handler,
unhandled exception filter) run to the exit of the process.

## Limits

- The emulated processors are those of one machine, run by one host thread: the processes and
  threads interleave (finely when they share memory), they do not use several host cores (only the
  rasterizer shares a batch of pixels between threads).
- Only the functions listed above are above the system calls: out-of-process COM servers, RPC to
  other processes and services, and the shell beyond `ShellExecute` (shell folders, the namespace)
  are answered as unavailable; GDI, GDI+, OpenGL and Direct3D draw what is listed above (the GDI+
  functions not listed, mesh and amplification shaders, the rarer DXIL operations, procedural
  geometry and intersection shaders, inline ray queries and local root signatures of ray tracing
  are not run, and are named in the summary; the rendering, in Python with numpy on a few threads,
  is far slower than a GPU); a service started with `StartService` is
  recorded, its program does not run.
- What the emulator lacks fails in the program (above): a program that needs it takes another
  path, or stops on its own error.
- A child of the other architecture needs `emulator_host` (in `hosts\`) and Fathom's `headless.exe`
  of that architecture; without them it ends at once with code 0, recorded as simulated.
- AVX-512 instructions are not emulated (a CPU with AVX-512 makes some runtimes use them).
- The stack of the dumped thread is committed in full when the dump is loaded, instead of growing
  through its guard page (unicorn cannot change a protection inside its protection fault): a stack
  overflow is an access violation at the end of the reserve, not `STATUS_STACK_OVERFLOW`. Another
  guard page loses its guard when it is touched, without `STATUS_GUARD_PAGE_VIOLATION`.

## Licenses

dumpulator: Boost Software License 1.0 ([`LICENSE.dumpulator`](LICENSE.dumpulator)), Copyright
Duncan Ogilvie. minidump: MIT ([`LICENSE.minidump`](LICENSE.minidump)), Copyright Tamas Jos
(skelsec). The changes to dumpulator and `avx.py`: Boost Software License 1.0 too, so that they can
go back to dumpulator. `fathom_emu.py` and the tests: GPL-3.0-or-later, like Fathom.
