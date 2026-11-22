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
  the events too.
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
- **Processes that run at the same time** (`processes.py`: the World of the run): the emulated
  processes run in turn by slices of 20,000 instructions, and when one waits for something another
  may do (a child's exit, a pipe, an event, a section), the others run until it can go on; their
  virtual clocks move together and jump to the first timeout only when every process waits. A child
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

- The processes of a run run in turn (slices of 20,000 instructions), not on separate processors:
  a race between two processes on shared memory is coarser than natively. Windows messages between
  processes (`SendMessage` to a window of another process) are not delivered.
- Only the functions listed above are above the system calls: out-of-process COM servers, RPC to
  other processes and services, and the shell beyond `ShellExecute` (shell folders, the namespace)
  are answered as unavailable; GDI draws what is listed above (no bitmaps, regions or paths); a
  service started with `StartService` is recorded, its program does not run.
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
