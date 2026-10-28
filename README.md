# x64dbg — EGKrb fork

<img width="100" src="src/bug_black.png" alt="x64dbg"/>

**Unofficial fork of [x64dbg](https://github.com/x64dbg/x64dbg)**, the open-source debugger for Windows (malware analysis, reverse-engineering of executables without source code). It tracks the official `development` branch and adds, for **malware triage, reverse-engineering and CTF**:

- **`TraceExport`** — turn execution traces into readable **JSON** or **CSV** that any tool can parse;
- **pybridge** — drive x64dbg **from Python** (breakpoints, single-stepping, registers, memory, traces);
- ready-to-use **script templates** for **`headless.exe`** (x64dbg with no GUI);
- a **memory dump viewer** (`.dmp`) and handy commands: **`.init`** (executable or dump), **`.save`**, **`scylla_hide.enable` / `.disable` / `.status`**;
- **HvKdFusion** — **Hyper-V kernel debugging and memory forensics** from the command bar: `hvkd` (LiveCloudKd + WinDbg/kd + MemProcFS + ntoseye) and **wineye**, a WinDbg-style REPL on kernel dumps, raw memory images and live VMs;
- the **ScyllaHide** and **xAnalyzer** plugins bundled in the release, **OllyDumpEx** installable with one command.

**[⬇ Download the latest release](https://github.com/EGKrb/x64dbg/releases/latest)** · [Official x64dbg README](README.x64dbg.md) · [Official docs](https://help.x64dbg.com) · 🇫🇷 [Lire en français](README.fr.md)

> **Platform: Windows (x86/x64).** x64dbg debugs Windows processes through the Windows debugging API, so the debugger, `headless.exe` and the plugins are Windows-only. The pybridge Python **client** is OS-agnostic (standard library only), but it drives an x64dbg instance — which runs on Windows.

---

## 60-second demo — drive the debugger from Python

```python
import sys
sys.path.insert(0, r"C:\Tools\x64dbg\release\pybridge\python")
from x64dbg_bridge import Debugger

with Debugger.launch(r"C:\Tools\x64dbg\release") as dbg:
    dbg.init(r"C:\Windows\System32\whoami.exe")            # start, wait for first pause

    # Log every DLL load without stopping the program
    dbg.set_breakpoint("kernelbase.LoadLibraryExW", break_=False,
                       log="LoadLibrary({utf16@arg.get(0)})")

    # Break on the first file open, then read the arguments
    dbg.set_breakpoint("kernelbase.CreateFileW")
    if dbg.run(wait=True, timeout=10):
        print("opening:", dbg.read_string(dbg.eval("arg.get(0)"), wide=True))
        print(dbg.regs())
```

Or export a trace to JSON without even running a process:

```
TraceExport C:\traces\sample.trace64, C:\traces\sample.json
```

```json
{"index": 1, "thread": 21712, "address": "0x7FF757A81290", "module": "sample.exe",
 "bytes": "E8 4B 02 00 00", "disasm": "call 0x00007FF757A814E0",
 "regs": {"rax": "0x7FF757A8128C", "rip": "0x7FF757A81290", "rflags": "0x202"},
 "mem": [{"address": "0x625451F978", "old": "0x0", "new": "0x7FF757A81295"}]}
```

---

## Contents

1. [Install](#install)
2. [Differences from official x64dbg](#differences-from-official-x64dbg)
3. [How x64dbg works](#how-x64dbg-works)
4. [TraceExport: exporting traces](#traceexport-exporting-traces)
5. [pybridge: driving x64dbg from Python](#pybridge-driving-x64dbg-from-python)
6. [Templates for headless.exe](#templates-for-headlessexe)
7. [Added commands and memory dumps](#added-commands-and-memory-dumps)
8. [HvKdFusion: Hyper-V kernel debugging and memory forensics](#hvkdfusion-hyper-v-kernel-debugging-and-memory-forensics)
9. [Bundled plugins](#bundled-plugins)
10. [Building it yourself](#building-it-yourself)
11. [Repository layout](#repository-layout)
12. [Licenses and credits](#licenses-and-credits)

---

## Install

1. Download `x64dbg-egkrb_<version>.zip` from the [releases page](https://github.com/EGKrb/x64dbg/releases/latest).
2. Extract it into a folder **you can write to** (e.g. `C:\Tools\x64dbg`, not `C:\Program Files`): x64dbg stores its configuration (`x64dbg.ini`) and databases (`db\`) next to the executables.
3. Run `release\x96dbg.exe` (launcher that picks the right architecture), or `release\x64\x64dbg.exe` directly for a 64-bit program and `release\x32\x32dbg.exe` for a 32-bit program.
4. Optional — install OllyDumpEx (not redistributable, see [Bundled plugins](#bundled-plugins)):
   ```powershell
   cd release
   powershell -ExecutionPolicy Bypass -File .\install-plugins.ps1 -Plugins OllyDumpEx
   ```

Archive contents:

```
release\
  x96dbg.exe                 launcher
  x64\  x32\                 64 / 32-bit debugger, headless.exe, plugins\
  x64\minidump.exe           memory dump viewer (.dmp)
  translations\              translated UI (French included: Options > Language)
  themes\                    dark theme
  pybridge\                  Python client, examples, headless templates
  install-plugins.ps1        installs/updates ScyllaHide, xAnalyzer, OllyDumpEx
  licenses\                  x64dbg and plugin licenses
  LISEZMOI.md                quick start
pluginsdk\                   headers and libs for writing plugins
commithash.txt               exact commit of the build
```

A second file, `x64dbg-egkrb_<version>_symbols.zip`, contains the debug symbols (`.pdb`), only useful to analyze a crash of x64dbg itself. `SHA256SUMS.txt` lists the hashes of both archives.

---

## Differences from official x64dbg

| | Official x64dbg | This fork |
|---|---|---|
| Code base | published releases (e.g. 2026.05.27) | recent `development` branch (features and fixes not yet in an official release) |
| Trace export | binary `.trace64`, readable only by x64dbg; CSV export only from the GUI | `TraceExport` command → JSON or CSV, also in scripts and `headless.exe` |
| Automation | x64dbg script language, C/C++ SDK | + Python client (pybridge) + headless templates with a launcher |
| Memory dumps (`.dmp`) | creation only (`minidump`) | + dump viewer (`minidump.exe`), opened with `.init file.dmp`; kernel dumps and raw memory images open in wineye |
| Kernel / whole machine | — (user-mode only) | HvKdFusion: Hyper-V VM memory read/write/dump, WinDbg/kd on a VM, MemProcFS mount, wineye REPL (`hvkd`, `wineye`) |
| Commands | — | `.init`, `.save`, `scylla_hide.enable` / `.disable` / `.status`, `hvkd`, `hvkd.log`, `hvkd.check`, `wineye` (ExtraCmds plugin) |
| Plugins | none | ScyllaHide and xAnalyzer bundled, pybridge and ExtraCmds bundled, OllyDumpEx via `install-plugins.ps1` |
| Build | — | fixes a `using namespace std` that broke the *unity build* (`patternfind.cpp`) |

The debugger core is unchanged: everything that works in official x64dbg works here identically. A [`feature/trace-export`](https://github.com/EGKrb/x64dbg/tree/feature/trace-export) branch holds only `TraceExport`, isolated from the rest.

> Based on a development build: it passes the project's automated test suite (81 tests on 64-bit), but may be less battle-tested than an official release.

---

## How x64dbg works

x64dbg is split into modules that communicate through a common layer, the *bridge*:

```
 x64dbg.exe / x32dbg.exe        headless.exe
   (Qt GUI, x64gui.dll)           (no GUI: console + scripts)
            \                      /
             x64bridge.dll  ── common API (DbgCmdExec, DbgMemRead, Gui*...)
                    |
              x64dbg.dll  ── the debugger: commands, expressions, scripts,
                    |        breakpoints, traces, database, plugins
                    |
        TitanEngine / GleeBug ── low-level engine (Windows debugging API)
                    |
              debugged process
```

- **The debugger (`x64dbg.dll`)** does all the work: it launches or attaches to the process, manages breakpoints (software `INT3`, hardware via debug registers, memory via page protections), evaluates expressions and runs commands.
- **The GUI** and **`headless.exe`** are just clients of the debugger. `headless.exe` reads commands from standard input, runs a script with `-cf`, and writes the log to standard output — that is what enables automation.
- **Everything is a command**: each GUI action maps to a text command (`bp`, `run`, `StepInto`, `savedata`...) usable in the command bar, in a script, or by a plugin.
- **Expressions** (`rax+8`, `[rsp]`, `kernel32.CreateFileW`, `mod.base(target.exe)`, `arg.get(0)`) are used wherever a value is expected. **Numbers are hexadecimal by default**; `.100` means 100 decimal.
- **Plugins** (`.dp64`/`.dp32` in `plugins\`) are loaded by the debugger, so also inside `headless.exe`. They can add commands and call the whole API.

### Traces

A **trace** records every instruction executed during automated single-stepping:

```
StartTraceRecording "C:\traces\target.trace64"   // open the recording file
TraceIntoConditional 0, .10000                    // trace until the condition is true (0 = never), max 10,000 instructions
StopTraceRecording
```

For each instruction, x64dbg writes to the `.trace64` file: the thread, the instruction bytes, the **registers changed** since the previous instruction (with a full state every 512 steps) and the **memory accesses** (address, old and new value). This compact format (described in [`docs/developers/tracefile.md`](docs/developers/tracefile.md)) is readable only by x64dbg — that is what `TraceExport` solves.

---

## TraceExport: exporting traces

```
TraceExport trace_file, output_file[, format]
```

| Argument | Description |
|---|---|
| `trace_file` | trace to read: `.trace64` with x64dbg, `.trace32` with x32dbg |
| `output_file` | created file (overwritten if it exists) |
| `format` | `json` or `csv`. Without this 3rd argument: `csv` if the name ends in `.csv`, otherwise `json` |

After the command, **`$result`** holds the number of exported instructions. The command **does not need a process being debugged**: it also works to convert old traces, and inside `headless.exe`.

### JSON format

```json
{
"version": 1,
"arch": "x64",
"path": "C:\\target.exe",
"hash": "0x5DD5453A5460CC36",
"instructions": [
{"index": 1, "thread": 21712, "address": "0x7FF757A81290", "module": "target.exe", "bytes": "E8 4B 02 00 00",
 "disasm": "call 0x00007FF757A814E0",
 "regs": {"rax": "0x7FF757A8128C", "rcx": "0x6254684000", "...": "...", "rip": "0x7FF757A81290", "rflags": "0x202"},
 "mem": [{"address": "0x625451F978", "old": "0x0", "new": "0x7FF757A81295"}]}
]
}
```

| Field | Content |
|---|---|
| `index` | instruction number in the trace (0, 1, 2...) |
| `thread` | thread id (decimal) |
| `address` | instruction address |
| `module` | module containing the address; present only if the trace belongs to the process being debugged |
| `bytes`, `disasm` | bytes and disassembly (Zydis) |
| `regs` | general registers, instruction pointer and flags **before** executing the instruction (`rax`…`r15`, `rip`, `rflags` on 64-bit; `eax`…`edi`, `eip`, `eflags` on 32-bit) |
| `mem` | the instruction's memory accesses: address, value before, value after (in pointer-sized blocks) |

Addresses and values are **hex strings**: a 64-bit integer does not fit in a standard JSON number. In Python: `int(value, 16)`. One instruction is written per line, so you can also `grep` the file.

### CSV format

One line per instruction, columns `index,thread,address,module,bytes,disasm,<registers>,memory`. The `memory` column lists accesses separated by `;`: `address:value` if memory was unchanged (read), `address:before->after` if it was modified. The file opens directly in Excel or with `pandas.read_csv`.

### How it works internally

[`src/dbg/TraceExport.cpp`](src/dbg/TraceExport.cpp) streams the file (constant memory, whatever the trace size):

1. checks the `TRAC` header and its JSON (version 1, same architecture as the debugger);
2. for each instruction block, applies the changed registers to the current state (registers are delta-encoded), reads the memory accesses, then disassembles the bytes;
3. skips user blocks (type ≥ 0x80) allowed by the format;
4. writes the JSON or CSV as it goes.

Safety: an output file identical to the input file is refused (it would be overwritten); a trace **still being recorded** can be exported, its incomplete last block is skipped with a warning; a corrupt file gives a precise message stating after how many instructions.

Limits: only general registers, the instruction pointer and flags are exported (not SSE/AVX/x87 registers, even though they are present in the trace); a 32-bit trace must be exported with x32dbg and vice versa.

Command docs: [`docs/commands/tracing/TraceExport.md`](docs/commands/tracing/TraceExport.md). Automated test: [`src/tests/trace_export/`](src/tests/trace_export).

---

## pybridge: driving x64dbg from Python

```
 Python script                         x64dbg / headless.exe
 x64dbg_bridge.py   ── TCP 127.0.0.1:27041 ──►  pybridge.dp64 plugin
 (standard library        one JSON request       (server thread)
  only)                   per line                     │
                    ◄── one JSON response ──   x64dbg API: commands,
                                               expressions, memory...
```

The plugin opens a server **on the local interface only** (127.0.0.1) and translates each request into an x64dbg API call. The Python client has **no dependencies**.

### Getting started

- `Debugger.launch(x64dbg_dir)` starts `headless.exe` (or `x64dbg.exe` with `gui=True`) with the `X64DBG_PYBRIDGE_PORT` variable, which starts the server automatically, then connects to it. At the end of the `with` block, debugging is stopped and `headless.exe` closed.
- In an already-open x64dbg: command `pybridge.start` (optional decimal port, 27041 by default), then `Debugger()` in Python. Also: `pybridge.stop`, `pybridge.status`.

```python
import sys
sys.path.insert(0, r"C:\Tools\x64dbg\release\pybridge\python")
from x64dbg_bridge import Debugger

with Debugger.launch(r"C:\Tools\x64dbg\release") as dbg:
    dbg.init(r"C:\Windows\System32\whoami.exe")           # start and wait for the first pause

    # Breakpoint that logs without stopping
    dbg.set_breakpoint("kernelbase.CreateFileW", break_=False,
                       log="CreateFileW({utf16@arg.get(0)})")

    # Breakpoint that stops
    dbg.set_breakpoint("kernelbase.LoadLibraryExW")
    if dbg.run(wait=True, timeout=10):
        print("DLL:", dbg.read_string(dbg.eval("arg.get(0)"), wide=True))
        print(dbg.regs())
        for ins in dbg.disasm("cip", 5):
            print(hex(ins["address"]), ins["text"])
```

### Python API

| Category | Method | Description |
|---|---|---|
| Session | `Debugger.launch(dir, arch="x64", gui=False, port, log_file)` | start x64dbg and connect |
| | `init(path, arguments, cwd)` / `attach(pid)` | launch / attach, wait for the first pause |
| | `stop()`, `close()` | stop debugging / close the session |
| Execution | `run(wait=False, timeout=10, pass_exceptions=False)` | resume; with `wait=True`, returns `True` if the program stopped again |
| | `pause()`, `step_into()`, `step_over()`, `step_out()` | return `True` once paused |
| | `wait(timeout)`, `state()` | wait for a pause; state `{debugging, running, cip}` |
| Registers | `regs()`, `reg(name)`, `set_reg(name, value)` | general registers, IP, flags |
| Memory | `read(addr, size)`, `write(addr, bytes)`, `read_ptr(addr)`, `read_string(addr, wide)`, `is_valid(addr)` | read/write (up to 64 MB per call) |
| | `disasm(addr, n)` | `n` instructions: address, size, text, bytes |
| Breakpoints | `set_breakpoint(addr, condition, log, command, break_, singleshot)` | software, with condition, automatic log text and command |
| | `set_hardware_breakpoint(addr, kind="x"/"w"/"r", size)`, `delete_breakpoint(addr)` | hardware; delete (all if `addr` omitted) |
| Traces | `trace(file, condition, max_steps, step_over)` | trace (and record) until the condition |
| | `export_trace(trace, output, fmt)` | `TraceExport`; returns the number of instructions |
| Commands | `cmd("text")`, `command(name, *args)`, `eval(expr)`, `result` | any x64dbg command; `command()` escapes text arguments |

Addresses and values accept an **integer** or an **x64dbg expression** (string); results are Python integers. Errors (unreadable memory, invalid expression, refused command) raise `BridgeError` with the x64dbg message.

### Bundled examples ([`pybridge/examples/`](pybridge/examples))

| Script | What it does |
|---|---|
| `api_monitor.py target.exe` | logs calls to CreateFileW, CreateProcessW, LoadLibraryExW, VirtualAlloc, VirtualProtect, WriteProcessMemory... with their arguments, without stopping the program |
| `trace_to_json.py target.exe --steps 20000 --out dir` | traces from the entry point, exports to JSON/CSV, prints the most-executed instructions and the number of memory writes |
| `dump_memory.py --target target.exe --addr "mod.base(target.exe)" --size "mod.size(target.exe)" --out m.bin` | saves a memory region (or `--pid` for an existing process, `--at` to wait for an address) |
| `step_log.py target.exe --count 50` | single-steps, printing the registers each instruction changed |

The examples use the x64dbg given by `--x64dbg` or the `X64DBG_DIR` environment variable.

### Protocol (for other languages)

One JSON request per line, one JSON response per line:

```
→ {"id": 1, "method": "mem_read", "params": {"addr": "rsp", "size": 16}}
← {"id": 1, "result": "00000000000000009512A857F77F0000"}
→ {"id": 2, "method": "mem_read", "params": {"addr": 0, "size": 4}}
← {"id": 2, "error": "cannot read memory"}
```

Methods: `ping`, `cmd`, `eval`, `state`, `wait`, `run`, `pause`, `step_into`, `step_over`, `step_out`, `stop`, `regs`, `reg_get`, `reg_set`, `mem_read`, `mem_write`, `mem_valid`, `disasm` (parameters in [`pybridge/src/api.cpp`](pybridge/src/api.cpp)).

### Security and limits

- **Any local program that connects to the port controls the debugger** (read/write the debugged process memory, run commands). The server listens on 127.0.0.1 only and starts on demand only; don't leave it running needlessly.
- One client at a time; requests are handled in order.
- `run()` does not block by default: use `wait=True` or `wait()` with a timeout, then `pause()` if needed.

---

## Templates for headless.exe

[`pybridge/headless/run-headless.ps1`](pybridge/headless/run-headless.ps1) runs an x64dbg script with no GUI:

```powershell
.\pybridge\headless\run-headless.ps1 -Template api-log -Target C:\Windows\System32\whoami.exe
.\pybridge\headless\run-headless.ps1 -Template trace-export -Target .\program.exe -Arch x32
```

How the launcher works:

1. replaces `%TARGET%` (full target path), `%MODULE%` (file name), `%OUT%` (output folder) and `%BITS%` (64 or 32) in the template;
2. creates `out\<template>-<date>\` with the final script (`script.txt`) and an **isolated config folder**: the template's `settingset` calls do not touch your configuration;
3. runs `headless.exe -userdir ... -cf script.txt`, prints and logs the output (`headless.log`);
4. closes `headless.exe` when the script prints `[template] done`, when the debugged program exits, or after `-TimeoutSeconds` (300 by default).

| Template | What it does | Files produced |
|---|---|---|
| `api-log` | logs API calls and their arguments, never stopping the program | `headless.log` (`[api]` lines) |
| `trace-export` | traces 50,000 instructions from the entry point and exports them | `trace.trace64`, `trace.json`, `trace.csv` |
| `dump-module` | saves the main module and a full minidump at the entry point | `<module>.mem`, `process.dmp` |
| `break-on-file` | stops only when the program opens a `.txt` file, then logs the call, saves the stack and writes a minidump; exits cleanly if no file matches | `stack.bin`, `at_createfile.dmp` |

The templates read arguments with `arg.get(n)`, so they work on both 64 and 32-bit. Breakpoints are set in `kernelbase.dll`, where the real API code lives (recent programs call it directly; `kernel32` only forwards).

### Writing your own template

A template is an ordinary x64dbg script:

```
// comment (or ;)
settingset Events, EntryBreakpoint, 1             // stop at the entry point
init "%TARGET%"                                    // launch the target, the script waits for the pause
bp kernelbase.VirtualProtect
bpcond kernelbase.VirtualProtect, "arg.get(2) == 40"   // only stop on PAGE_EXECUTE_READWRITE
erun                                               // continue; the script resumes at the next pause
cmp $pid, 0                                        // $pid = 0: the program exited
je end
savedata "%OUT%\region.bin", arg.get(0), arg.get(1)
end:
log "[template] done"
```

Log text format: `{x:rax}` hex, `{d:...}` decimal, `{p:...}` pointer, `{a:addr}` address with symbol, `{utf16@addr}` / `{utf8@addr}` string, `{i:addr}` instruction. See the [command documentation](https://help.x64dbg.com/en/latest/commands/).

---

## Added commands and memory dumps

The **ExtraCmds** plugin ([`extracmds/`](extracmds/src/extracmds.cpp)) adds these commands, usable in the command bar, scripts, `headless.exe` and pybridge:

| Command | Effect |
|---|---|
| `.init file.exe[, arguments[, dir]]` | launches and debugs the program (same as `init`) |
| `.init file.dmp` | opens the dump in the **dump viewer** (see below); a **kernel** crash dump (`MEMORY.DMP`) or a `.raw`/`.mem`/`.vmem` image opens in **wineye** ([HvKdFusion](#hvkdfusion-hyper-v-kernel-debugging-and-memory-forensics)) |
| `.save` | saves the program database (comments, labels, breakpoints) — like `dbsave` |
| `.save file.dmp` | writes a full process minidump — like `minidump` |
| `.save file.dd64` | saves the database to that file — like `dbsave file` |
| `.save file, address` | saves the memory region containing the address (`mem.base` / `mem.size`) |
| `.save file, address, size` | saves a memory range — like `savedata` (size in hex: `100` = 256 bytes, `.256` decimal) |
| `scylla_hide.status` | shows whether ScyllaHide is active, its profile and the available profiles |
| `scylla_hide.enable [profile]` | enables ScyllaHide: given profile (exact name or prefix, e.g. `vmprotect`, `basic`), else the last active profile, else "Basic" |
| `scylla_hide.disable` | disables ScyllaHide ("Disabled" profile) |
| `hvkd [action [options]]`, `hvkd.log`, `hvkd.check`, `wineye` | HvKdFusion, see [below](#hvkdfusion-hyper-v-kernel-debugging-and-memory-forensics) |

```
scylla_hide.enable basic
.init "C:\target.exe"
.save "C:\out\target.dmp"
.save "C:\out\stack.bin", rsp
.init "C:\out\target.dmp"
```

How it works:

- **ScyllaHide** only reads its config at load time. `scylla_hide.enable/disable` edits `plugins\scylla_hide.ini` (`CurrentProfile`) then reloads the plugin (`plugunload` + `plugload`): the profile applies immediately, without restarting x64dbg. Protections are injected **at program start or attach**: if a program is already being debugged, restart it to pick up the new profile.
- **A dump is not a process**: x64dbg debugs running programs and cannot "load" a `.dmp` (no execution, stepping or breakpoints on a dump). `.init file.dmp` therefore opens the **dump viewer** `x64\minidump.exe`, built from x64dbg's official code (`src/cross/minidump`): memory map, disassembly, hex view, threads and registers. It reads 32 and 64-bit process dumps and can also run standalone (`minidump.exe file.dmp`). To analyze a dump with commands and scripts, use WinDbg (`cdb -z file.dmp`).

---

## HvKdFusion: Hyper-V kernel debugging and memory forensics

x64dbg stops at user mode. **HvKdFusion** ([`hvkdfusion/`](hvkdfusion/README.md), merged from [EGKrb/HvKdFusion](https://github.com/EGKrb/HvKdFusion)) covers the kernel and the whole memory of a machine — a **live Hyper-V VM** or a **memory image** — by tying together LiveCloudKd (`hvlib.dll`/`hvmm.sys`, Hyper-V guest memory), WinDbg/kd, MemProcFS + LeechCore (+ Dokany to mount memory as a drive) and ntoseye, under one Python CLI. It adds **wineye**, a native WinDbg-style REPL (`ps`, `lm`, `!process`, `x nt!...`, `db/dq`, `dt`, `!vad`, `!handle`, `s`) on `vmm.dll`, without WSL or Dokany, with freeze/resume/registers on a live VM.

| Command (ExtraCmds) | Effect |
|---|---|
| `hvkd` | interactive menu in a console: pick a VM, then debug (WinDbg/kd/EXDI), dump, read, write, resume, ntoseye, mount |
| `hvkd action [options]` | any HvKdFusion action in a console, e.g. `hvkd dump --vm 0 --out C:\dumps\vm0.raw`, `hvkd debug --vm 0 --engine windbg` |
| `hvkd.log action [options]` | same, without a window, output in the x64dbg log: `hvkd.log list`, `hvkd.log read --vm 0 --addr 0x1000 --size 0x100` |
| `hvkd.check [--deep]` | readiness table of every component, in the log |
| `wineye file` / `wineye --live --vm 0` | wineye on a kernel dump / raw image, or on a live VM |

```
hvkd.check
hvkd dump --vm 0 --type linear --out C:\dumps\vm0.raw
.init C:\dumps\vm0.raw
wineye --live --vm 0
```

Memory operations on a VM need administrator rights: `hvkd` asks for elevation (UAC) when needed. The third-party components are **not bundled** (licenses, kernel drivers): install or build them, then run `python hvkdfusion.py setup` once — see [hvkdfusion/README.md](hvkdfusion/README.md). Requires Python 3.11+ on the `PATH`.

---

## Bundled plugins

| Plugin | Version | Role | Usage |
|---|---|---|---|
| **ScyllaHide** | v1.4 | hides the debugger from anti-debug techniques (PEB, `NtQueryInformationProcess`, timings, exceptions...) by injecting hooks into the process | menu *Plugins > ScyllaHide > Options*. Default active profile: "VMProtect x86/x64"; switch to "Basic" if an unprotected program misbehaves |
| **xAnalyzer** | 2.5.12 | static analysis: recognizes API calls and comments their arguments in the disassembly, detects loops and functions | menu *Plugins > xAnalyzer* or right-click in the disassembly, or commands `xanal selection` / `xanal function` / `xanal module` (`xanalremove ...` to clear); API definitions in `plugins\apis_def\` |
| **OllyDumpEx** | v1.86 | dumps a process to a runnable PE file (for packed/protected programs), complementing the built-in Scylla | menu *Plugins > OllyDumpEx* |
| **pybridge** | 1 | control from Python | see [pybridge](#pybridge-driving-x64dbg-from-python) |
| **ExtraCmds** | 1 | commands `.init`, `.save`, `scylla_hide.*` | see [Added commands](#added-commands-and-memory-dumps) |

OllyDumpEx is free closed-source software **without a redistribution license**: it is therefore not included in the release. [`install-plugins.ps1`](plugins/install-plugins.ps1) downloads it from its author's site.

```powershell
.\install-plugins.ps1                              # all three plugins (skips already-installed ones)
.\install-plugins.ps1 -Plugins OllyDumpEx          # just one
.\install-plugins.ps1 -X64dbgDir C:\x64dbg\release -Force   # other folder, reinstall
```

Each downloaded file is verified by its **SHA-256 hash** (list in [`plugins/THIRD-PARTY.md`](plugins/THIRD-PARTY.md)): if a file changed at its author's site, installation stops instead of installing an unknown file. The script uses `curl.exe -4` (IPv4), more reliable than `Invoke-WebRequest` on networks where IPv6 is broken.

> If you install xAnalyzer by hand: the API definitions must be in `plugins\apis_def\`, otherwise the plugin refuses to load (*"Failed to locate API definitions files"*).

---

## Building it yourself

Requirements: Windows 10/11, **Visual Studio 2022** (C++ workload), **CMake** ≥ 3.15, **Ninja**, **git**, Python 3 (for the tests).

```bat
git clone --recursive https://github.com/EGKrb/x64dbg
cd x64dbg

:: 64-bit, in an "x64 Native Tools" terminal (or after vcvars64.bat)
cmake -B build64 -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_UNITY_BUILD=ON -DCMAKE_UNITY_BUILD_BATCH_SIZE=6
cmake --build build64

:: 32-bit, in an "x86 Native Tools" terminal (or after vcvars32.bat)
cmake -B build32 -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_UNITY_BUILD=ON -DCMAKE_UNITY_BUILD_BATCH_SIZE=6
cmake --build build32
```

The executables are produced in `bin\x64` and `bin\x32` (Qt and dependencies are copied automatically).

```bat
:: pybridge and ExtraCmds plugins (same terminal as the target architecture; build-x32 for 32-bit)
cd pybridge
cmake -B build-x64 -G Ninja -DCMAKE_BUILD_TYPE=Release -DX64DBG_SDK=<pluginsdk folder>
cmake --build build-x64
cd ..\extracmds
cmake -B build-x64 -G Ninja -DCMAKE_BUILD_TYPE=Release -DX64DBG_SDK=<pluginsdk folder>
cmake --build build-x64
```

By default the plugins use the `pluginsdk` of an x64dbg installed via winget; otherwise, point to one from a release (`-DX64DBG_SDK=...\pluginsdk`).

```powershell
# Dump viewer ("x64 Native Tools" terminal, after the 64-bit build whose Qt it reuses)
.\tools\build-minidump-viewer.ps1                    # produces build-cross-x64\minidump.exe, to copy into bin\x64
```

```powershell
.\plugins\install-plugins.ps1                       # third-party plugins into bin\x32 and bin\x64
py src\tests\run.py --arch x64                       # test suite (including trace_export)
.\tools\make-release.ps1 -Version 2026.10.03        # release archives into dist\
```

### Updating from official x64dbg

```bat
git remote add upstream https://github.com/x64dbg/x64dbg
git fetch upstream
git merge upstream/development
git submodule update --init --recursive
```

---

## Repository layout

Files specific to this fork (the rest is identical to official x64dbg):

```
src/dbg/TraceExport.cpp, .h          TraceExport command
src/dbg/commands/cmd-tracing.cpp     command declaration
src/tests/trace_export/              automated test
docs/commands/tracing/TraceExport.md command documentation
pybridge/
  src/                               plugin (server, API)
  python/x64dbg_bridge.py            Python client
  examples/                          Python examples
  headless/                          headless launcher and templates
extracmds/src/extracmds.cpp          ExtraCmds plugin (.init, .save, scylla_hide.*, hvkd*, wineye)
hvkdfusion/                          HvKdFusion (Hyper-V kernel debugging / memory forensics, Python)
  hvkdfusion.py, wineye.py, hvkd.ps1 CLI, WinDbg-style REPL, elevated launcher
plugins/
  install-plugins.ps1                third-party plugin installer
  THIRD-PARTY.md                     versions, licenses, hashes
tools/
  make-release.ps1                   release archive builder
  build-minidump-viewer.ps1          dump viewer build (src/cross/minidump)
  LISEZMOI.md                        quick start shipped in the archive
README.x64dbg.md                     official x64dbg README
README.fr.md                         this README in French
```

---

## Licenses and credits

- **x64dbg** and this fork: [GPL-3.0](LICENSE). Source for each release: the commit listed in `commithash.txt`.
- **ScyllaHide**: GPL-3.0 — <https://github.com/x64dbg/ScyllaHide> (v1.4 source: tag `v1.4`).
- **xAnalyzer**: MIT — <https://github.com/ThunderCls/xAnalyzer>, by ThunderCls.
- **OllyDumpEx**: freeware by Low Priority — <https://low-priority.appspot.com/ollydumpex/>, not redistributed.
- **HvKdFusion**: GPL-3.0-or-later — <https://github.com/EGKrb/HvKdFusion>. The components it drives are not redistributed: LiveCloudKd (GPL-3.0), MemProcFS and LeechCore (AGPL-3.0), Dokany (LGPL/MIT), ntoseye, WinDbg (Microsoft license).
- **Dump viewer**: x64dbg code (GPL-3.0); libraries [udmp-parser](https://github.com/0vercl0k/udmp-parser) (MIT), [cpp-httplib](https://github.com/yhirose/cpp-httplib) (MIT), [linux-pe](https://github.com/can1357/linux-pe) (BSD), [nlohmann/json](https://github.com/nlohmann/json) (MIT), Qt 5.12 (LGPL-3.0, DLLs shared with x64dbg).

x64dbg is developed by mrexodia and contributors ([list](https://github.com/x64dbg/x64dbg/graphs/contributors)); see the [official README](README.x64dbg.md) for full credits. This fork is neither affiliated with nor endorsed by the x64dbg team; for official builds, go to <https://x64dbg.com>.
